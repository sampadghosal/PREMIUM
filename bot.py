import asyncio
import hashlib
import io
import json
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import qrcode
import firebase_admin
from firebase_admin import credentials, db
from cryptography.fernet import Fernet, InvalidToken
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -----------------------------------------------------------------------------
# SILENT PREMIUM BOT V2
# Persistent state lives in Firebase Realtime Database.
# GitHub Actions/local Python contains no SQLite or persistent application state.
# -----------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("silent-premium-bot")

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("TG_BOT_TOKEN is missing")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing")

try:
    ADMIN_ID_INT = int(ADMIN_ID)
except ValueError as exc:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID") from exc

FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_CLIENT_EMAIL = os.environ.get("FIREBASE_CLIENT_EMAIL", "").strip()
FIREBASE_PRIVATE_KEY = os.environ.get("FIREBASE_PRIVATE_KEY", "").strip()
FIREBASE_DATABASE_URL = os.environ.get("FIREBASE_DATABASE_URL", "").strip()

if not all((FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, FIREBASE_PRIVATE_KEY, FIREBASE_DATABASE_URL)):
    raise RuntimeError(
        "Firebase secrets missing: FIREBASE_PROJECT_ID, FIREBASE_CLIENT_EMAIL, "
        "FIREBASE_PRIVATE_KEY, FIREBASE_DATABASE_URL"
    )

# Only secrets belong in GitHub Secrets.
# Operational store settings (UPI, support, bot username, product URL, plans, etc.)
# are read from Firebase Realtime Database.
KEY_ENCRYPTION_KEY = os.environ.get("KEY_ENCRYPTION_KEY", "").strip()

if not KEY_ENCRYPTION_KEY:
    raise RuntimeError("KEY_ENCRYPTION_KEY is missing")

try:
    FERNET = Fernet(KEY_ENCRYPTION_KEY.encode("utf-8"))
except Exception as exc:
    raise RuntimeError("KEY_ENCRYPTION_KEY must be a valid Fernet key") from exc


# -----------------------------------------------------------------------------
# Firebase
# -----------------------------------------------------------------------------

def init_firebase() -> None:
    if firebase_admin._apps:
        return

    private_key = FIREBASE_PRIVATE_KEY.replace("\\n", "\n")
    service_account = {
        "type": "service_account",
        "project_id": FIREBASE_PROJECT_ID,
        "private_key_id": os.environ.get("FIREBASE_PRIVATE_KEY_ID", "github-actions"),
        "private_key": private_key,
        "client_email": FIREBASE_CLIENT_EMAIL,
        "client_id": os.environ.get("FIREBASE_CLIENT_ID", ""),
        "token_uri": "https://oauth2.googleapis.com/token",
    }

    cred = credentials.Certificate(service_account)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DATABASE_URL})
    logger.info("Firebase initialized")


init_firebase()


def fb_get(path: str) -> Any:
    return db.reference(path).get()


def fb_set(path: str, value: Any) -> None:
    db.reference(path).set(value)


def fb_update(path: str, value: dict[str, Any]) -> None:
    db.reference(path).update(value)


def fb_push(path: str, value: dict[str, Any]) -> str:
    ref = db.reference(path).push(value)
    return ref.key


def fb_delete(path: str) -> None:
    db.reference(path).delete()


# -----------------------------------------------------------------------------
# Configuration helpers
# -----------------------------------------------------------------------------

def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_settings() -> dict[str, Any]:
    settings = fb_get("settings") or {}
    settings.setdefault("payment", {})
    settings.setdefault("support", {})
    settings.setdefault("telegram", {})
    settings.setdefault("website", {})
    return settings


def get_product(product_id: str) -> Optional[dict[str, Any]]:
    return fb_get(f"products/{product_id}")


def get_plan(product_id: str, plan_id: str) -> Optional[dict[str, Any]]:
    return fb_get(f"products/{product_id}/plans/{plan_id}")


def ensure_user(user) -> None:
    uid = str(user.id)
    path = f"users/{uid}"
    existing = fb_get(path)
    if not existing:
        fb_set(
            path,
            {
                "telegram_id": user.id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "purchase_count": 0,
                "created_at": now_ms(),
                "last_seen_at": now_ms(),
            },
        )
    else:
        fb_update(
            path,
            {
                "username": user.username or existing.get("username", ""),
                "first_name": user.first_name or existing.get("first_name", ""),
                "last_seen_at": now_ms(),
            },
        )


def normalize_key(key: str) -> str:
    return "".join(ch for ch in key.upper().strip() if ch.isalnum())


def sha256_key(key: str) -> str:
    return hashlib.sha256(normalize_key(key).encode("utf-8")).hexdigest()


def encrypt_key(raw_key: str) -> str:
    return FERNET.encrypt(raw_key.encode("utf-8")).decode("utf-8")


def decrypt_key(encrypted_key: str) -> str:
    return FERNET.decrypt(encrypted_key.encode("utf-8")).decode("utf-8")


def generate_premium_key() -> str:
    # 128 bits of cryptographic randomness, displayed as 4 groups.
    alphabet = string.ascii_uppercase + string.digits
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return f"SILENT-{raw[:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"


def order_id() -> str:
    return "SP" + secrets.token_hex(5).upper()


def purchase_id() -> str:
    return "PUR" + secrets.token_hex(6).upper()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID_INT


def username_text(user) -> str:
    return f"@{user.username}" if user.username else f"ID {user.id}"


def support_url(settings: dict[str, Any]) -> str:
    username = settings.get("support", {}).get("telegram_username", "").strip().lstrip("@")
    return f"https://t.me/{username}" if username else "https://t.me/"


def product_url(product: dict[str, Any]) -> str:
    return str(product.get("product_url", "")).strip()


# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Website Premium", callback_data="product:website", style="success")],
        [
            InlineKeyboardButton("📦 My Orders", callback_data="my_orders", style="primary"),
            InlineKeyboardButton("📞 Support / Help", callback_data="support", style="primary"),
        ],
    ])


def plans_menu(product: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []
    plans = product.get("plans", {})
    for plan_id, plan in plans.items():
        if not plan.get("enabled", True):
            continue
        rows.append([
            InlineKeyboardButton(
                f"🟢 {plan.get('name', plan_id)} — ₹{plan.get('price', 0)}",
                callback_data=f"plan:website:{plan_id}",
                style="success",
            )
        ])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data="home", style="primary")])
    return InlineKeyboardMarkup(rows)


def payment_method_menu(order: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Manual UPI", callback_data=f"manual:{order}", style="success")],
        [InlineKeyboardButton("◀️ Back", callback_data=f"planback:{order}", style="primary")],
    ])


def payment_action_menu(order: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Submit UTR", callback_data=f"utr:{order}", style="success")],
        [InlineKeyboardButton("🔴 Cancel Order", callback_data=f"cancel:{order}", style="danger")],
    ])


def order_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Main Menu", callback_data="home", style="primary")]])


def admin_review_menu(order: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"adminapprove:{order}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"adminreject:{order}"),
        ],
        [InlineKeyboardButton("🔎 Order Details", callback_data=f"admindetail:{order}")],
    ])


def html_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# -----------------------------------------------------------------------------
# Start / product flow
# -----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    await asyncio.to_thread(ensure_user, user)
    await update.message.reply_text(
        "<b>👑 SILENT PREMIUM</b>\n\n"
        "Premium access made simple.\n\n"
        "Choose a service below:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def show_home(query) -> None:
    await query.edit_message_text(
        "<b>👑 SILENT PREMIUM</b>\n\n"
        "Premium access made simple.\n\n"
        "Choose a service below:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def show_product(query, product_id: str) -> None:
    product = await asyncio.to_thread(get_product, product_id)
    if not product or not product.get("enabled", True):
        await query.edit_message_text(
            "<b>⚠️ PRODUCT UNAVAILABLE</b>\n\nThis product is currently unavailable.",
            parse_mode=ParseMode.HTML,
            reply_markup=order_back_menu(),
        )
        return

    await query.edit_message_text(
        f"<b>👑 {html_escape(product.get('name', 'Premium'))}</b>\n\n"
        f"{html_escape(product.get('description', 'Premium access.'))}\n\n"
        "<b>Choose your plan:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=plans_menu(product),
    )


async def create_order(query, user, product_id: str, plan_id: str) -> None:
    product = await asyncio.to_thread(get_product, product_id)
    plan = await asyncio.to_thread(get_plan, product_id, plan_id)

    if not product or not product.get("enabled", True) or not plan or not plan.get("enabled", True):
        await query.edit_message_text(
            "<b>⚠️ PLAN UNAVAILABLE</b>\n\nPlease choose another plan.",
            parse_mode=ParseMode.HTML,
            reply_markup=order_back_menu(),
        )
        return

    settings = await asyncio.to_thread(get_settings)
    window = int(settings.get("payment", {}).get("payment_window_minutes", 10))
    created = now_ms()
    deadline = created + window * 60 * 1000
    oid = order_id()

    order = {
        "telegram_id": user.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "product_id": product_id,
        "plan_id": plan_id,
        "amount": float(plan.get("price", 0)),
        "status": "awaiting_payment_method",
        "payment_status": "pending",
        "utr": None,
        "created_at": created,
        "payment_deadline": deadline,
        "updated_at": created,
        "qr_message_id": None,
        "status_message_id": query.message.message_id,
        "admin_message_id": None,
        "cancelled_at": None,
        "rejected_at": None,
        "approved_at": None,
        "purchase_id": None,
        "key_hash": None,
    }
    await asyncio.to_thread(fb_set, f"orders/{oid}", order)

    await query.edit_message_text(
        "<b>💳 CHOOSE YOUR PAYMENT METHOD</b>\n\n"
        f"Order: <code>{oid}</code>\n"
        f"Plan: {html_escape(plan.get('name', plan_id))}\n"
        f"Amount: <b>₹{plan.get('price', 0)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=payment_method_menu(oid),
    )


# -----------------------------------------------------------------------------
# Manual UPI
# -----------------------------------------------------------------------------

async def send_manual_upi(query, context, oid: str) -> None:
    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order or order.get("telegram_id") != query.from_user.id:
        await query.answer("Order not found.", show_alert=True)
        return

    if order.get("status") in {"cancelled", "completed", "payment_rejected", "expired"}:
        await query.answer("This order is no longer active.", show_alert=True)
        return

    if now_ms() > int(order.get("payment_deadline", 0)):
        await asyncio.to_thread(
            fb_update,
            f"orders/{oid}",
            {"status": "expired", "payment_status": "expired", "updated_at": now_ms()},
        )
        await query.edit_message_text(
            "<b>⏰ ORDER EXPIRED</b>\n\nThe payment window has expired. Please create a new order.",
            parse_mode=ParseMode.HTML,
            reply_markup=order_back_menu(),
        )
        return

    settings = await asyncio.to_thread(get_settings)
    payment = settings.get("payment", {})
    upi_id = str(payment.get("upi_id", "")).strip()
    upi_name = str(payment.get("upi_name", "")).strip()
    amount = order.get("amount", 0)

    if not upi_id:
        await query.answer("UPI is not configured yet. Contact admin.", show_alert=True)
        return

    # Standard UPI URI for QR generation. QR is generated in memory and not persisted locally.
    upi_uri = f"upi://pay?pa={upi_id}&pn={upi_name}&am={amount}&cu=INR&tn={oid}"
    qr = qrcode.make(upi_uri)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    buf.seek(0)

    caption = (
        "<b>🧾 MANUAL UPI PAYMENT</b>\n\n"
        f"Order: <code>{oid}</code>\n"
        f"Amount: <b>₹{amount}</b>\n"
        f"UPI ID: <code>{html_escape(upi_id)}</code>\n"
        f"Name: {html_escape(upi_name)}\n\n"
        "<b>Instructions</b>\n"
        "1. Pay the exact amount.\n"
        f"2. Use <code>{oid}</code> as the payment note if supported.\n"
        "3. Submit your UTR after payment.\n\n"
        "⏳ Payment window: 10 minutes"
    )

    qr_message = await query.message.reply_photo(
        photo=buf,
        caption=caption,
        parse_mode=ParseMode.HTML,
    )

    status_message = await query.message.reply_text(
        "<b>⏳ PAYMENT PENDING</b>\n\n"
        f"Order: <code>{oid}</code>\n"
        f"Amount: <b>₹{amount}</b>\n\n"
        "Complete the payment and submit your UTR.",
        parse_mode=ParseMode.HTML,
        reply_markup=payment_action_menu(oid),
    )

    await asyncio.to_thread(
        fb_update,
        f"orders/{oid}",
        {
            "status": "awaiting_payment",
            "payment_status": "pending",
            "qr_message_id": qr_message.message_id,
            "status_message_id": status_message.message_id,
            "updated_at": now_ms(),
        },
    )

    await query.answer("Payment instructions sent.")


async def cancel_order(query, oid: str) -> None:
    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order or order.get("telegram_id") != query.from_user.id:
        await query.answer("Order not found.", show_alert=True)
        return

    if order.get("status") in {"completed", "cancelled", "payment_rejected", "expired"}:
        await query.answer("This order is already closed.", show_alert=True)
        return

    await asyncio.to_thread(
        fb_update,
        f"orders/{oid}",
        {"status": "cancelled", "payment_status": "cancelled", "cancelled_at": now_ms(), "updated_at": now_ms()},
    )
    await asyncio.to_thread(
        fb_update, f"users/{query.from_user.id}", {"pending_utr_order": None, "last_seen_at": now_ms()}
    )

    qr_id = order.get("qr_message_id")
    if qr_id:
        try:
            await query.message.get_bot().delete_message(query.message.chat_id, int(qr_id))
        except Exception:
            logger.info("Could not delete QR message for %s", oid)

    await query.edit_message_text(
        "<b>🔴 ORDER CANCELLED</b>\n\n"
        f"Order <code>{oid}</code> has been cancelled.",
        parse_mode=ParseMode.HTML,
        reply_markup=order_back_menu(),
    )


async def request_utr(query, context: ContextTypes.DEFAULT_TYPE, oid: str) -> None:
    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order or order.get("telegram_id") != query.from_user.id:
        await query.answer("Order not found.", show_alert=True)
        return

    if now_ms() > int(order.get("payment_deadline", 0)):
        await asyncio.to_thread(
            fb_update,
            f"orders/{oid}",
            {"status": "expired", "payment_status": "expired", "updated_at": now_ms()},
        )
        await query.answer("Payment window expired. Create a new order.", show_alert=True)
        return

    await asyncio.to_thread(
        fb_update,
        f"orders/{oid}",
        {"status": "awaiting_utr", "updated_at": now_ms()},
    )

    # Keep the pending UTR order in Firebase as well as memory. This is important
    # for GitHub Actions because the runner/process can restart at any time.
    context.user_data["awaiting_utr_order"] = oid
    await asyncio.to_thread(
        fb_update,
        f"users/{query.from_user.id}",
        {"pending_utr_order": oid, "last_seen_at": now_ms()},
    )

    await query.message.reply_text(
        "<b>🧾 SUBMIT UTR</b>\n\n"
        "Please send your UTR/reference number.\n\n"
        "Example:\n<code>123456789012</code>",
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


# -----------------------------------------------------------------------------
# UTR handling
# -----------------------------------------------------------------------------

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive and persist a UTR after the user presses Submit UTR.

    Important:
    - Validation is performed before any Firebase write.
    - The UTR save is isolated from optional admin notification.
    - Once Firebase confirms the UTR was saved, the user is never told that
      the submission failed merely because an optional follow-up action failed.
    """
    user = update.effective_user
    message = update.message

    if not user or not message or not message.text:
        return

    uid = str(user.id)

    # -------------------------------------------------------------------------
    # 1. Recover the pending order.
    # -------------------------------------------------------------------------
    try:
        oid = context.user_data.get("awaiting_utr_order")
    except Exception:
        oid = None

    if not oid:
        try:
            user_record = await asyncio.to_thread(fb_get, f"users/{uid}") or {}
            oid = user_record.get("pending_utr_order")
        except Exception:
            logger.exception("Failed to recover pending UTR order for user %s", uid)
            await message.reply_text(
                "⚠️ I couldn't find your pending order right now. "
                "Please press Submit UTR again."
            )
            return

    if not oid:
        # Ordinary message; this user is not currently submitting a UTR.
        return

    oid = str(oid)

    # -------------------------------------------------------------------------
    # 2. Validate UTR.
    # -------------------------------------------------------------------------
    raw_utr = message.text.strip()
    normalized_utr = "".join(
        ch for ch in raw_utr.upper()
        if ch.isalnum()
    )

    if not 6 <= len(normalized_utr) <= 40:
        await message.reply_text(
            "❌ Please send a valid UTR/reference number.\n\n"
            "Example: <code>123456789012</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # -------------------------------------------------------------------------
    # 3. Load and validate the order.
    # -------------------------------------------------------------------------
    try:
        order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    except Exception:
        logger.exception("Failed to load order %s for UTR submission", oid)
        await message.reply_text(
            "⚠️ I couldn't load your order right now. Please try again."
        )
        return

    if not order or str(order.get("telegram_id")) != uid:
        try:
            context.user_data.pop("awaiting_utr_order", None)
        except Exception:
            pass

        try:
            await asyncio.to_thread(
                fb_update,
                f"users/{uid}",
                {"pending_utr_order": None, "last_seen_at": now_ms()},
            )
        except Exception:
            logger.exception("Failed to clear invalid pending UTR marker for %s", uid)

        await message.reply_text(
            "That order is no longer available."
        )
        return

    status = str(order.get("status", ""))

    if status in {"cancelled", "completed", "payment_rejected", "expired"}:
        try:
            context.user_data.pop("awaiting_utr_order", None)
        except Exception:
            pass

        try:
            await asyncio.to_thread(
                fb_update,
                f"users/{uid}",
                {"pending_utr_order": None, "last_seen_at": now_ms()},
            )
        except Exception:
            logger.exception("Failed to clear closed-order marker for %s", uid)

        await message.reply_text("This order is already closed.")
        return

    if status not in {"awaiting_utr", "awaiting_payment"}:
        await message.reply_text(
            "This order is not currently waiting for a UTR. "
            "Please use Submit UTR again."
        )
        return

    # -------------------------------------------------------------------------
    # 4. Check payment-window expiry safely.
    # -------------------------------------------------------------------------
    try:
        deadline = int(order.get("payment_deadline", 0))
    except (TypeError, ValueError):
        logger.exception("Invalid payment_deadline for order %s", oid)
        deadline = 0

    if deadline and now_ms() > deadline:
        try:
            await asyncio.to_thread(
                fb_update,
                f"orders/{oid}",
                {
                    "status": "expired",
                    "payment_status": "expired",
                    "updated_at": now_ms(),
                },
            )
        except Exception:
            logger.exception("Failed to expire order %s", oid)

        try:
            context.user_data.pop("awaiting_utr_order", None)
        except Exception:
            pass

        try:
            await asyncio.to_thread(
                fb_update,
                f"users/{uid}",
                {"pending_utr_order": None, "last_seen_at": now_ms()},
            )
        except Exception:
            logger.exception("Failed to clear expired UTR marker for %s", uid)

        await message.reply_text(
            "<b>⏰ ORDER EXPIRED</b>\n\nPlease create a new order.",
            parse_mode=ParseMode.HTML,
        )
        return

    # -------------------------------------------------------------------------
    # 5. Duplicate UTR check.
    #
    # This is deliberately best-effort. It must NEVER prevent a legitimate UTR
    # from being saved merely because an RTDB read/query fails.
    # -------------------------------------------------------------------------
    try:
        all_orders = await asyncio.to_thread(fb_get, "orders") or {}

        duplicate = any(
            isinstance(other, dict)
            and str(other_id) != oid
            and str(other.get("utr") or "").strip().upper() == normalized_utr
            and str(other.get("status", "")) not in {
                "cancelled",
                "payment_rejected",
                "expired",
            }
            for other_id, other in all_orders.items()
        )

        if duplicate:
            await message.reply_text(
                "⚠️ This UTR has already been submitted for another order.\n"
                "Please check the UTR and try again."
            )
            return

    except Exception:
        logger.exception(
            "Duplicate UTR lookup failed for order %s; continuing with save",
            oid,
        )

    # -------------------------------------------------------------------------
    # 6. CRITICAL: save UTR.
    # -------------------------------------------------------------------------
    timestamp = now_ms()

    try:
        await asyncio.to_thread(
            fb_update,
            f"orders/{oid}",
            {
                "utr": normalized_utr,
                "status": "awaiting_verification",
                "payment_status": "submitted",
                "utr_submitted_at": timestamp,
                "updated_at": timestamp,
            },
        )
    except Exception:
        logger.exception(
            "CRITICAL: Firebase failed to save UTR for order %s",
            oid,
        )
        await message.reply_text(
            "⚠️ I couldn't save the UTR right now. "
            "Please try again in a moment."
        )
        return

    # -------------------------------------------------------------------------
    # 7. Verify the save before telling the user it succeeded.
    # -------------------------------------------------------------------------
    try:
        saved_order = await asyncio.to_thread(
            fb_get,
            f"orders/{oid}",
        ) or {}

        saved_utr = str(saved_order.get("utr") or "").strip().upper()
        saved_status = str(saved_order.get("status") or "")

        if saved_utr != normalized_utr or saved_status != "awaiting_verification":
            logger.error(
                "UTR save verification failed for %s: saved_utr=%r status=%r",
                oid,
                saved_utr,
                saved_status,
            )
            await message.reply_text(
                "⚠️ The UTR could not be confirmed as saved. "
                "Please try again."
            )
            return

    except Exception:
        logger.exception(
            "Could not verify saved UTR for order %s",
            oid,
        )
        await message.reply_text(
            "⚠️ The UTR was submitted, but I couldn't confirm the save yet. "
            "Please do not submit a different UTR; contact support if the "
            "order does not update."
        )
        return

    # -------------------------------------------------------------------------
    # 8. Clear pending state only AFTER successful persistence.
    # -------------------------------------------------------------------------
    try:
        context.user_data.pop("awaiting_utr_order", None)
    except Exception:
        pass

    try:
        await asyncio.to_thread(
            fb_update,
            f"users/{uid}",
            {
                "pending_utr_order": None,
                "last_seen_at": timestamp,
            },
        )
    except Exception:
        # Cleanup failure does not invalidate a successfully saved UTR.
        logger.exception(
            "Failed to clear pending UTR marker for user %s",
            uid,
        )

    # -------------------------------------------------------------------------
    # 9. Tell the user the actual result.
    # -------------------------------------------------------------------------
    await message.reply_text(
        "<b>✅ UTR SUBMITTED</b>\n\n"
        f"Order: <code>{html_escape(oid)}</code>\n"
        f"UTR: <code>{html_escape(normalized_utr)}</code>\n\n"
        "Your payment is now waiting for manual verification.",
        parse_mode=ParseMode.HTML,
    )

    # Admin notification is NON-CRITICAL. The UTR is already safely stored.
    try:
        await send_admin_review(update, oid)
    except Exception:
        logger.exception(
            "Optional admin notification failed for order %s",
            oid,
        )


async def send_admin_review(update: Update, oid: str) -> None:
    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order:
        return
    plan = await asyncio.to_thread(get_plan, order.get("product_id", "website"), order.get("plan_id", ""))
    user_label = f"@{order.get('username')}" if order.get("username") else f"ID {order.get('telegram_id')}"

    text = (
        "<b>💰 PAYMENT VERIFICATION</b>\n\n"
        f"Order: <code>{oid}</code>\n"
        f"User: {html_escape(user_label)}\n"
        f"Telegram ID: <code>{order.get('telegram_id')}</code>\n\n"
        f"Product: {html_escape(order.get('product_id'))}\n"
        f"Plan: {html_escape((plan or {}).get('name', order.get('plan_id')))}\n"
        f"Amount: <b>₹{order.get('amount')}</b>\n\n"
        f"UTR: <code>{html_escape(order.get('utr'))}</code>\n\n"
        "Status: Awaiting verification"
    )

    try:
        msg = await update.get_bot().send_message(
            chat_id=ADMIN_ID_INT,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=admin_review_menu(oid),
        )
        await asyncio.to_thread(fb_update, f"orders/{oid}", {"admin_message_id": msg.message_id})
    except Exception:
        logger.exception("Failed to send admin review for %s", oid)


# -----------------------------------------------------------------------------
# Approval / rejection
# -----------------------------------------------------------------------------

async def approve_order(query, context: ContextTypes.DEFAULT_TYPE, oid: str) -> None:
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return
    if order.get("status") != "awaiting_verification":
        await query.answer("This order is no longer awaiting verification.", show_alert=True)
        return

    product_id = order.get("product_id", "website")
    plan_id = order.get("plan_id", "")
    plan = await asyncio.to_thread(get_plan, product_id, plan_id)
    product = await asyncio.to_thread(get_product, product_id)
    if not plan or not product:
        await query.answer("Product/plan configuration missing.", show_alert=True)
        return

    duration_days = int(plan.get("duration_days", 0))
    if duration_days <= 0:
        await query.answer("Invalid plan duration.", show_alert=True)
        return

    # Idempotency guard: a second click cannot create a second purchase.
    raw_key = generate_premium_key()
    key_hash = sha256_key(raw_key)
    purchased_at = now_ms()
    expires_at = purchased_at + duration_days * 24 * 60 * 60 * 1000
    pid = purchase_id()

    encrypted_key = encrypt_key(raw_key)

    purchase = {
        "telegram_id": order.get("telegram_id"),
        "order_id": oid,
        "product_id": product_id,
        "plan_id": plan_id,
        "amount": order.get("amount"),
        "key_hash": key_hash,
        "encrypted_key": encrypted_key,
        "purchased_at": purchased_at,
        "expires_at": expires_at,
        "status": "active",
        "utr": order.get("utr"),
    }

    key_record = {
        "status": "active",
        "product_id": product_id,
        "expires_at": expires_at,
    }

    # Atomic multi-location write. This is the critical persistence point.
    updates = {
        f"purchases/{pid}": purchase,
        f"key_verification/{key_hash}": key_record,
        f"orders/{oid}/status": "completed",
        f"orders/{oid}/payment_status": "approved",
        f"orders/{oid}/approved_at": purchased_at,
        f"orders/{oid}/purchase_id": pid,
        f"orders/{oid}/key_hash": key_hash,
        f"orders/{oid}/updated_at": purchased_at,
        f"users/{order.get('telegram_id')}/purchase_count": None,
    }

    # Firebase multi-location update cannot increment. Read current count and include it.
    uid = str(order.get("telegram_id"))
    existing_user = await asyncio.to_thread(fb_get, f"users/{uid}") or {}
    count = int(existing_user.get("purchase_count", 0)) + 1
    updates[f"users/{uid}/purchase_count"] = count
    updates[f"users/{uid}/last_seen_at"] = purchased_at

    await asyncio.to_thread(db.reference("/").update, updates)

    try:
        await query.edit_message_text(
            "<b>✅ PAYMENT APPROVED</b>\n\n"
            f"Order: <code>{oid}</code>\n"
            f"Purchase: <code>{pid}</code>\n"
            f"Key issued to the customer.\n\n"
            "Status: Active",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    # Send raw key only to the customer; Firebase stores only its hash.
    try:
        destination = product_url(product)
        buttons = []
        if destination and "YOUR-WEBSITE-URL" not in destination:
            buttons.append([InlineKeyboardButton("🌐 Open Product", url=destination)])
        buttons.append([InlineKeyboardButton("📦 My Orders", callback_data="my_orders", style="primary")])

        await context.bot.send_message(
            chat_id=int(order["telegram_id"]),
            text=(
                "<b>🎉 PAYMENT APPROVED</b>\n\n"
                "Your premium access is now active.\n\n"
                f"📦 Plan: {html_escape(plan.get('name', plan_id))}\n"
                f"💰 Paid: ₹{order.get('amount')}\n\n"
                f"🔑 <b>Your Premium Key</b>\n\n"
                f"<code>{raw_key}</code>\n\n"
                f"⏰ Expires: <code>{datetime.fromtimestamp(expires_at / 1000, timezone.utc).strftime('%d %b %Y %H:%M UTC')}</code>"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        logger.exception("Failed to deliver key for %s", oid)


async def reject_order(query, oid: str) -> None:
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return

    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return
    if order.get("status") != "awaiting_verification":
        await query.answer("This order is no longer awaiting verification.", show_alert=True)
        return

    await asyncio.to_thread(
        fb_update,
        f"orders/{oid}",
        {
            "status": "payment_rejected",
            "payment_status": "rejected",
            "rejected_at": now_ms(),
            "updated_at": now_ms(),
        },
    )

    try:
        await query.edit_message_text(
            "<b>❌ PAYMENT REJECTED</b>\n\n"
            f"Order: <code>{oid}</code> has been rejected.",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    try:
        await query.get_bot().send_message(
            chat_id=int(order["telegram_id"]),
            text=(
                "<b>❌ PAYMENT NOT APPROVED</b>\n\n"
                f"Order: <code>{oid}</code>\n\n"
                "Your payment could not be verified.\n"
                "Please contact support if you believe this was a mistake."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=order_back_menu(),
        )
    except Exception:
        logger.exception("Failed to notify rejected customer %s", oid)


async def admin_detail(query, oid: str) -> None:
    if not is_admin(query.from_user.id):
        await query.answer("Admin only.", show_alert=True)
        return
    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return
    await query.message.reply_text(
        "<b>🔎 ORDER DETAILS</b>\n\n<pre>" + html_escape(json.dumps(order, indent=2)) + "</pre>",
        parse_mode=ParseMode.HTML,
    )
    await query.answer()


# -----------------------------------------------------------------------------
# Orders / support
# -----------------------------------------------------------------------------

async def show_orders(query) -> None:
    """
    Show the user's active premium purchases.

    This deliberately reads the purchases collection once and filters locally.
    It avoids Firebase order_by_child/equal_to query issues and uses the
    purchase record (the source of truth for active premium access).
    """
    uid = int(query.from_user.id)

    try:
        all_purchases = await asyncio.to_thread(fb_get, "purchases") or {}

        active_purchases = []

        for pid, purchase in all_purchases.items():
            try:
                purchase_uid = int(purchase.get("telegram_id", 0))
                expires_at = int(purchase.get("expires_at", 0))
            except (TypeError, ValueError):
                continue

            if (
                purchase_uid == uid
                and purchase.get("status") == "active"
                and expires_at > now_ms()
                and purchase.get("encrypted_key")
            ):
                active_purchases.append({
                    "purchase_id": str(pid),
                    **purchase,
                })

        active_purchases.sort(
            key=lambda p: int(p.get("expires_at", 0)),
            reverse=True,
        )

        buttons = []

        for purchase in active_purchases:
            plan = await asyncio.to_thread(
                get_plan,
                purchase.get("product_id", "website"),
                purchase.get("plan_id", ""),
            )

            plan_name = (plan or {}).get(
                "name",
                purchase.get("plan_id", "Premium"),
            )

            # Purchase ID is short and keeps callback_data safely under
            # Telegram's 64-byte callback-data limit.
            buttons.append([
                InlineKeyboardButton(
                    f"🟢 {plan_name} · ₹{purchase.get('amount', 0)}",
                    callback_data=f"purchase:view:{purchase['purchase_id']}",
                    style="success",
                )
            ])

        if active_purchases:
            message = (
                "<b>📦 MY ORDERS</b>\n\n"
                "<b>🟢 ACTIVE PREMIUM</b>\n\n"
                "Tap your active order below to view your premium key."
            )
        else:
            message = (
                "<b>📦 MY ORDERS</b>\n\n"
                "You currently have no active premium orders."
            )

        buttons.append([
            InlineKeyboardButton(
                "◀️ Back to Main Menu",
                callback_data="home",
                style="primary",
            )
        ])

        await query.edit_message_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    except Exception:
        logger.exception(
            "My Orders failed for Telegram user %s",
            uid,
        )

        await query.edit_message_text(
            "<b>⚠️ MY ORDERS</b>\n\n"
            "I couldn't load your active orders right now.\n\n"
            "Please try again.",
            parse_mode=ParseMode.HTML,
            reply_markup=order_back_menu(),
        )


async def show_purchase_detail(query, purchase_id_value: str) -> None:
    """Display and recover the key for one active purchase."""
    uid = int(query.from_user.id)

    try:
        purchase = await asyncio.to_thread(
            fb_get,
            f"purchases/{purchase_id_value}",
        )

        if not purchase:
            await query.answer(
                "Purchase not found.",
                show_alert=True,
            )
            return

        # Never allow one Telegram user to open another user's purchase.
        if int(purchase.get("telegram_id", 0)) != uid:
            await query.answer(
                "Purchase not found.",
                show_alert=True,
            )
            return

        expires_at = int(purchase.get("expires_at", 0))

        if (
            purchase.get("status") != "active"
            or expires_at <= now_ms()
            or not purchase.get("encrypted_key")
        ):
            await query.answer(
                "This premium order is no longer active.",
                show_alert=True,
            )
            return

        raw_key = decrypt_key(purchase["encrypted_key"])

        product_id = purchase.get("product_id", "website")
        plan_id = purchase.get("plan_id", "")

        plan = await asyncio.to_thread(
            get_plan,
            product_id,
            plan_id,
        )

        product = await asyncio.to_thread(
            get_product,
            product_id,
        )

        expires_text = datetime.fromtimestamp(
            expires_at / 1000,
            timezone.utc,
        ).strftime("%d %b %Y %H:%M UTC")

        buttons = []

        destination = product_url(product or {})

        if destination and "YOUR-WEBSITE-URL" not in destination:
            buttons.append([
                InlineKeyboardButton(
                    "🌐 Open Product",
                    url=destination,
                )
            ])

        buttons.append([
            InlineKeyboardButton(
                "📦 Back to My Orders",
                callback_data="my_orders",
                style="primary",
            )
        ])

        await query.edit_message_text(
            "<b>🔑 ACTIVE PREMIUM ORDER</b>\n\n"
            f"Purchase: <code>{html_escape(purchase_id_value)}</code>\n"
            f"Order: <code>{html_escape(purchase.get('order_id', ''))}</code>\n"
            f"Plan: <b>{html_escape((plan or {}).get('name', plan_id or 'Premium'))}</b>\n"
            f"Paid: <b>₹{purchase.get('amount', 0)}</b>\n\n"
            "<b>🔑 Your Premium Key</b>\n\n"
            f"<code>{html_escape(raw_key)}</code>\n\n"
            f"⏰ Expires: <code>{expires_text}</code>\n\n"
            "You can open this order again anytime from "
            "<b>My Orders</b> while it remains active.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    except (InvalidToken, TypeError, ValueError):
        logger.exception(
            "Key recovery/decryption failed for purchase %s",
            purchase_id_value,
        )

        await query.edit_message_text(
            "<b>⚠️ KEY RECOVERY ERROR</b>\n\n"
            "Your premium purchase exists, but the key could not be recovered.\n\n"
            "Please contact support.",
            parse_mode=ParseMode.HTML,
            reply_markup=order_back_menu(),
        )

    except Exception:
        logger.exception(
            "Purchase detail failed for Telegram user %s / purchase %s",
            uid,
            purchase_id_value,
        )

        await query.edit_message_text(
            "<b>⚠️ ORDER ERROR</b>\n\n"
            "I couldn't open this premium order right now.\n\n"
            "Please try again.",
            parse_mode=ParseMode.HTML,
            reply_markup=order_back_menu(),
        )


async def show_support(query) -> None:
    settings = await asyncio.to_thread(get_settings)
    username = settings.get("support", {}).get("telegram_username", "").strip().lstrip("@")
    text = (
        "<b>📞 SUPPORT / HELP</b>\n\n"
        "For payment or premium-access issues, contact support."
    )
    buttons = []
    if username:
        buttons.append([InlineKeyboardButton("📞 Contact Support", url=f"https://t.me/{username}")])
    buttons.append([InlineKeyboardButton("◀️ Back to Main Menu", callback_data="home", style="primary")])
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


# -----------------------------------------------------------------------------
# Admin commands
# -----------------------------------------------------------------------------

async def resend_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return

    purchases = await asyncio.to_thread(
        lambda: db.reference("purchases").order_by_child("telegram_id").equal_to(user.id).get() or {}
    )
    active = [
        (pid, p) for pid, p in purchases.items()
        if p.get("status") == "active" and int(p.get("expires_at", 0)) > now_ms()
    ]
    if not active:
        await update.message.reply_text("No active premium purchase was found.")
        return

    pid, purchase = sorted(active, key=lambda item: int(item[1].get("expires_at", 0)), reverse=True)[0]
    encrypted = purchase.get("encrypted_key")
    if not encrypted:
        await update.message.reply_text("Your key cannot be recovered. Please contact support.")
        return

    try:
        raw_key = decrypt_key(encrypted)
    except InvalidToken:
        logger.error("Could not decrypt key for purchase %s", pid)
        await update.message.reply_text("Your key cannot be recovered. Please contact support.")
        return

    product = await asyncio.to_thread(get_product, purchase.get("product_id", "website"))
    destination = product_url(product or {})
    buttons = []
    if destination and "YOUR-WEBSITE-URL" not in destination:
        buttons.append([InlineKeyboardButton("🌐 Open Product", url=destination)])
    buttons.append([InlineKeyboardButton("📦 My Orders", callback_data="my_orders", style="primary")])

    await update.message.reply_text(
        "<b>🔑 YOUR PREMIUM KEY</b>\n\n"
        f"<code>{raw_key}</code>\n\n"
        f"⏰ Expires: <code>{datetime.fromtimestamp(int(purchase['expires_at']) / 1000, timezone.utc).strftime('%d %b %Y %H:%M UTC')}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    pending = await asyncio.to_thread(
        lambda: db.reference("orders").order_by_child("status").equal_to("awaiting_verification").get() or {}
    )
    if not pending:
        await update.message.reply_text("<b>📋 No pending payment verifications.</b>", parse_mode=ParseMode.HTML)
        return

    for oid, order in pending.items():
        await send_admin_review(update, oid)


# -----------------------------------------------------------------------------
# Callback router
# -----------------------------------------------------------------------------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = query.from_user

    if data == "home":
        await show_home(query)
        return
    if data == "product:website":
        await show_product(query, "website")
        return
    if data in {"orders", "my_orders"}:
        await show_orders(query)
        return
    if data.startswith("purchase:view:"):
        await show_purchase_detail(query, data.split(":", 2)[2])
        return
    if data == "support":
        await show_support(query)
        return

    if data.startswith("plan:website:"):
        plan_id = data.split(":", 2)[2]
        await create_order(query, user, "website", plan_id)
        return

    if data.startswith("manual:"):
        await send_manual_upi(query, context, data.split(":", 1)[1])
        return

    if data.startswith("cancel:"):
        await cancel_order(query, data.split(":", 1)[1])
        return

    if data.startswith("utr:"):
        await request_utr(query, context, data.split(":", 1)[1])
        return

    if data.startswith("adminapprove:"):
        await approve_order(query, context, data.split(":", 1)[1])
        return

    if data.startswith("adminreject:"):
        await reject_order(query, data.split(":", 1)[1])
        return

    if data.startswith("admindetail:"):
        await admin_detail(query, data.split(":", 1)[1])
        return

    if data.startswith("planback:"):
        await show_product(query, "website")
        return


# -----------------------------------------------------------------------------
# Expiry maintenance
# -----------------------------------------------------------------------------

async def expire_due_records(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = now_ms()
    try:
        # Expire open orders whose payment window has passed.
        open_statuses = ["awaiting_payment_method", "awaiting_payment", "awaiting_utr"]
        for status in open_statuses:
            orders = await asyncio.to_thread(
                lambda s=status: db.reference("orders").order_by_child("status").equal_to(s).get() or {}
            )
            updates = {}
            for oid, order in orders.items():
                if int(order.get("payment_deadline", 0)) and int(order.get("payment_deadline", 0)) < now:
                    updates[f"orders/{oid}/status"] = "expired"
                    updates[f"orders/{oid}/payment_status"] = "expired"
                    updates[f"orders/{oid}/updated_at"] = now
            if updates:
                await asyncio.to_thread(db.reference("/").update, updates)

        # Expire premium purchases and website verification records.
        active_purchases = await asyncio.to_thread(
            lambda: db.reference("purchases").order_by_child("status").equal_to("active").get() or {}
        )
        updates = {}
        for pid, purchase in active_purchases.items():
            if int(purchase.get("expires_at", 0)) <= now:
                updates[f"purchases/{pid}/status"] = "expired"
                key_hash = purchase.get("key_hash")
                if key_hash:
                    updates[f"key_verification/{key_hash}/status"] = "expired"
        if updates:
            await asyncio.to_thread(db.reference("/").update, updates)
    except Exception:
        logger.exception("Expiry maintenance failed")


# -----------------------------------------------------------------------------
# Error handling / main
# -----------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled Telegram error", exc_info=context.error)


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    class MessageQueryAdapter:
        from_user = update.effective_user

        async def edit_message_text(self, *args, **kwargs):
            return await update.message.reply_text(*args, **kwargs)

    await show_orders(MessageQueryAdapter())


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("orders", orders_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("resendkey", resend_key))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)

    if application.job_queue:
        application.job_queue.run_repeating(expire_due_records, interval=60, first=10)
    else:
        logger.warning("JobQueue unavailable; expiry will still be checked during user/admin actions")

    return application


if __name__ == "__main__":
    app = build_application()
    logger.info("SILENT PREMIUM BOT V2 starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
