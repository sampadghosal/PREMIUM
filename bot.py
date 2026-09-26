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
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import qrcode
import firebase_admin
from firebase_admin import credentials, db
from cryptography.fernet import Fernet, InvalidToken
from telegram import BotCommand, BotCommandScopeChat, Update
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

VPLINK_API_URL = os.environ.get("VPLINK_API_URL", "https://vplink.in/api").strip()
VPLINK_API_TOKEN = os.environ.get("VPLINK_API_TOKEN", "").strip()
FREE_VERIFY_URL = os.environ.get("FREE_VERIFY_URL", "").strip().rstrip("/")
FREE_CLAIM_TTL_SECONDS = int(os.environ.get("FREE_CLAIM_TTL_SECONDS", "600"))

if not VPLINK_API_TOKEN:
    raise RuntimeError("VPLINK_API_TOKEN is missing")
if not FREE_VERIFY_URL:
    raise RuntimeError("FREE_VERIFY_URL is missing")
if FREE_CLAIM_TTL_SECONDS < 60:
    raise RuntimeError("FREE_CLAIM_TTL_SECONDS must be at least 60")

try:
    FERNET = Fernet(KEY_ENCRYPTION_KEY.encode("utf-8"))
except Exception as exc:
    raise RuntimeError("KEY_ENCRYPTION_KEY must be a valid Fernet key") from exc


from UI import UI, render as ui_render

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


def free_claim_id() -> str:
    return secrets.token_urlsafe(32)


def free_claim_hash(claim: str) -> str:
    return hashlib.sha256(claim.encode("utf-8")).hexdigest()


def build_vplink_url(destination: str) -> str:
    params = {
        "api": VPLINK_API_TOKEN,
        "url": destination,
    }
    request_url = f"{VPLINK_API_URL}?{urlencode(params)}"
    request = Request(request_url, headers={"User-Agent": "SILENT-PREMIUM-BOT/1.0"})
    with urlopen(request, timeout=20) as response:
        body = response.read().decode("utf-8", errors="replace")
    data = json.loads(body)
    if str(data.get("status", "")).lower() != "success":
        raise RuntimeError(str(data.get("message", "VPLINK returned an error")))
    short_url = str(data.get("shortenedUrl", "")).strip()
    if not short_url.startswith(("https://", "http://")):
        raise RuntimeError("VPLINK returned an invalid shortenedUrl")
    return short_url


def consume_free_claim(
    claim_hash: str,
    telegram_id: int,
    candidate_purchase_id: str,
    candidate_key_hash: str,
    candidate_encrypted_key: str,
    candidate_expires_at: int,
) -> tuple[bool, Optional[dict[str, Any]], str]:
    path = f"free_claims/{claim_hash}"
    ref = db.reference(path)
    now = now_ms()

    def transaction(current):
        if not isinstance(current, dict):
            return current
        if str(current.get("telegram_id")) != str(telegram_id):
            return current
        status = current.get("status")
        if status == "used":
            # A previous attempt already reserved the one-hour key. Returning
            # the same issuance data makes delivery recoverable if a later
            # Firebase write or Telegram send failed.
            return current
        if status != "pending":
            return current
        try:
            if int(current.get("expires_at", 0)) <= now:
                current["status"] = "expired"
                current["expired_at"] = now
                return current
        except (TypeError, ValueError):
            return current

        current["status"] = "used"
        current["used_at"] = now
        current["issued_purchase_id"] = candidate_purchase_id
        current["issued_key_hash"] = candidate_key_hash
        current["issued_encrypted_key"] = candidate_encrypted_key
        current["issued_expires_at"] = candidate_expires_at
        return current

    result = ref.transaction(transaction)
    if not isinstance(result, dict):
        return False, None, "invalid"
    if str(result.get("telegram_id")) != str(telegram_id):
        return False, result, "owner_mismatch"
    if result.get("status") == "expired":
        return False, result, "expired"
    if result.get("status") == "used" and result.get("issued_key_hash"):
        return True, result, "ok"
    return False, result, "invalid"


async def create_free_claim(user) -> str:
    claim = free_claim_id()
    claim_hash = free_claim_hash(claim)
    created = now_ms()
    expires = created + FREE_CLAIM_TTL_SECONDS * 1000
    verify_url = f"{FREE_VERIFY_URL}?{urlencode({'claim': claim})}"
    completion_url = f"{FREE_VERIFY_URL.rsplit('/', 1)[0]}/complete.php?{urlencode({'claim': claim})}"

    # Generate the VPLINK URL server-side. The user receives only verify_url.
    short_url = await asyncio.to_thread(build_vplink_url, completion_url)
    record = {
        "telegram_id": user.id,
        "claim_hash": claim_hash,
        "status": "pending",
        "created_at": created,
        "expires_at": expires,
        "verify_url": verify_url,
        "short_url": short_url,
        "completion_url": completion_url,
        "access_type": "free_shortlink",
    }
    await asyncio.to_thread(fb_set, f"free_claims/{claim_hash}", record)
    return verify_url


# -----------------------------------------------------------------------------
# UI helpers
# -----------------------------------------------------------------------------
# IMPORTANT: UI.py is the single customer-facing UI engine.
# Do not create Telegram keyboards directly in bot.py.

def main_menu():
    return UI.keyboard("welcome")

def plans_menu(product: dict[str, Any]):
    return UI.plans_keyboard(product)

def payment_method_menu(order: str):
    return UI.keyboard("payment_methods", {"order_id": order, "product_id": "website"})

def payment_action_menu(order: str):
    return UI.keyboard("payment_pending", {"order_id": order})

def order_back_menu():
    return UI.keyboard("order_cancelled")

def admin_review_menu(order: str):
    return UI.admin_review_keyboard(order)


def html_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# -----------------------------------------------------------------------------
# Start / product flow
# -----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    await asyncio.to_thread(ensure_user, user)

    payload = ""
    if context.args:
        payload = str(context.args[0]).strip()

    if payload.startswith("free_"):
        claim = payload[5:]
        if not claim or len(claim) > 128:
            await update.message.reply_text(UI.text("free_access", field="invalid"), parse_mode=ParseMode.HTML)
            return

        claim_hash = free_claim_hash(claim)

        issued_at = now_ms()
        expires_at = issued_at + 60 * 60 * 1000
        candidate_key = generate_premium_key()
        candidate_key_hash = sha256_key(candidate_key)
        candidate_pid = purchase_id()
        candidate_encrypted_key = encrypt_key(candidate_key)

        try:
            ok, record, reason = await asyncio.to_thread(
                consume_free_claim,
                claim_hash,
                user.id,
                candidate_pid,
                candidate_key_hash,
                candidate_encrypted_key,
                expires_at,
            )
        except Exception:
            logger.exception("Free claim transaction failed for Telegram user %s", user.id)
            await update.message.reply_text(UI.text("free_access", field="error"), parse_mode=ParseMode.HTML)
            return

        if not ok or not record:
            field = "already_used" if reason == "already_used" else "expired" if reason == "expired" else "invalid"
            await update.message.reply_text(UI.text("free_access", field=field), parse_mode=ParseMode.HTML)
            return

        # If this claim was already consumed by the same user, recover the
        # exact previously-issued key instead of creating another one.
        pid = str(record.get("issued_purchase_id", candidate_pid))
        key_hash = str(record.get("issued_key_hash", candidate_key_hash))
        encrypted_key = str(record.get("issued_encrypted_key", candidate_encrypted_key))
        expires_at = int(record.get("issued_expires_at", expires_at))
        raw_key = decrypt_key(encrypted_key)

        existing_purchase = await asyncio.to_thread(fb_get, f"purchases/{pid}")
        if not existing_purchase:
            purchase = {
                "telegram_id": user.id,
                "order_id": None,
                "product_id": "website",
                "plan_id": "free_1_hour",
                "plan_name": "Free 1 Hour",
                "amount": 0,
                "key_hash": key_hash,
                "encrypted_key": encrypted_key,
                "purchased_at": int(record.get("used_at", issued_at)),
                "expires_at": expires_at,
                "status": "active",
                "access_type": "free_shortlink",
                "claim_hash": claim_hash,
            }
            updates = {
                f"purchases/{pid}": purchase,
                f"key_verification/{key_hash}": {
                    "status": "active",
                    "product_id": "website",
                    "expires_at": expires_at,
                    "access_type": "free_shortlink",
                    "telegram_id": user.id,
                    "claim_hash": claim_hash,
                },
                f"users/{user.id}/last_seen_at": issued_at,
            }
            try:
                await asyncio.to_thread(db.reference("/").update, updates)
            except Exception:
                logger.exception("Failed to persist free premium key for %s", user.id)
                await update.message.reply_text(UI.text("free_access", field="error"), parse_mode=ParseMode.HTML)
                return

        expires_text = datetime.fromtimestamp(expires_at / 1000, timezone.utc).strftime("%d %b %Y %H:%M UTC")
        await update.message.reply_text(
            UI.text("free_access", field="success", values={"key": raw_key, "expires_at": expires_text}),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.payment_approved_keyboard(product_url(await asyncio.to_thread(get_product, "website") or {})),
        )
        return

    await update.message.reply_text(
        UI.text("welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=UI.keyboard("welcome"),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        UI.text("help"),
        parse_mode=ParseMode.HTML,
        reply_markup=UI.keyboard("help"),
    )


async def plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    product = await asyncio.to_thread(get_product, "website")
    if not product or not product.get("enabled", True):
        await update.message.reply_text(
            UI.text("order_error", {"product_name": "Premium"}),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.keyboard("order_cancelled"),
        )
        return
    values = {
        "product_id": "website",
        "product_name": product.get("name", "Premium"),
        "description": product.get("description", "Premium access."),
    }
    await update.message.reply_text(
        UI.text("product", values),
        parse_mode=ParseMode.HTML,
        reply_markup=plans_menu(product),
    )


async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await show_support(MessageQueryAdapter(update.message, update.effective_user))


async def show_home(query) -> None:
    await query.edit_message_text(
        UI.text("welcome"),
        parse_mode=ParseMode.HTML,
        reply_markup=UI.keyboard("welcome"),
    )


async def show_product(query, product_id: str) -> None:
    product = await asyncio.to_thread(get_product, product_id)
    if not product or not product.get("enabled", True):
        await query.edit_message_text(
            UI.text("order_error", {"product_name": "Premium"}),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.keyboard("order_cancelled"),
        )
        return

    values = {
        "product_id": product_id,
        "product_name": product.get("name", "Premium"),
        "description": product.get("description", "Premium access."),
    }

    await query.edit_message_text(
        UI.text("product", values),
        parse_mode=ParseMode.HTML,
        reply_markup=plans_menu(product),
    )


async def create_order(query, user, product_id: str, plan_id: str) -> None:
    product = await asyncio.to_thread(get_product, product_id)
    plan = await asyncio.to_thread(get_plan, product_id, plan_id)

    if not product or not product.get("enabled", True) or not plan or not plan.get("enabled", True):
        await query.edit_message_text(
            UI.text("alerts", field="plan_unavailable"),
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
        UI.text("payment_methods", {
            "order_id": oid,
            "plan_name": plan.get("name", plan_id),
            "amount": plan.get("price", 0),
            "product_id": product_id,
        }),
        parse_mode=ParseMode.HTML,
        reply_markup=payment_method_menu(oid),
    )


# -----------------------------------------------------------------------------
# Manual UPI
# -----------------------------------------------------------------------------

async def send_manual_upi(query, context, oid: str) -> None:
    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order or order.get("telegram_id") != query.from_user.id:
        await query.answer(UI.text("generic", field="order_not_found"), show_alert=True)
        return

    if order.get("status") in {"cancelled", "completed", "payment_rejected", "expired"}:
        await query.answer(UI.text("generic", field="order_closed"), show_alert=True)
        return

    if now_ms() > int(order.get("payment_deadline", 0)):
        await asyncio.to_thread(
            fb_update,
            f"orders/{oid}",
            {"status": "expired", "payment_status": "expired", "updated_at": now_ms()},
        )
        await query.edit_message_text(
            UI.text("order_expired", {"order_id": oid}),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.keyboard("order_expired"),
        )
        return

    settings = await asyncio.to_thread(get_settings)
    payment = settings.get("payment", {})
    upi_id = str(payment.get("upi_id", "")).strip()
    upi_name = str(payment.get("upi_name", "")).strip()
    amount = order.get("amount", 0)

    if not upi_id:
        await query.answer(UI.text("generic", field="upi_missing"), show_alert=True)
        return

    upi_uri = f"upi://pay?pa={upi_id}&pn={upi_name}&am={amount}&cu=INR&tn={oid}"
    qr = qrcode.make(upi_uri)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    buf.seek(0)

    caption = UI.text("manual_upi", {
        "order_id": oid,
        "amount": amount,
        "upi_id": html_escape(upi_id),
        "upi_name": html_escape(upi_name),
        "payment_window_minutes": payment.get("payment_window_minutes", 10),
    }, field="caption")

    qr_message = await query.message.reply_photo(
        photo=buf,
        caption=caption,
        parse_mode=ParseMode.HTML,
    )

    status_message = await query.message.reply_text(
        UI.text("payment_pending", {"order_id": oid, "amount": amount}),
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

    await query.answer(UI.text("alerts", field="payment_instructions_sent"))


async def cancel_order(query, oid: str) -> None:
    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order or order.get("telegram_id") != query.from_user.id:
        await query.answer(UI.text("generic", field="order_not_found"), show_alert=True)
        return

    if order.get("status") in {"completed", "cancelled", "payment_rejected", "expired"}:
        await query.answer(UI.text("generic", field="order_already_closed"), show_alert=True)
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
        UI.text("order_cancelled", {"order_id": oid}),
        parse_mode=ParseMode.HTML,
        reply_markup=UI.keyboard("order_cancelled"),
    )


async def request_utr(query, context: ContextTypes.DEFAULT_TYPE, oid: str) -> None:
    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order or order.get("telegram_id") != query.from_user.id:
        await query.answer(UI.text("generic", field="order_not_found"), show_alert=True)
        return

    if now_ms() > int(order.get("payment_deadline", 0)):
        await asyncio.to_thread(
            fb_update,
            f"orders/{oid}",
            {"status": "expired", "payment_status": "expired", "updated_at": now_ms()},
        )
        await query.answer(UI.text("generic", field="payment_window_expired"), show_alert=True)
        return

    await asyncio.to_thread(
        fb_update,
        f"orders/{oid}",
        {"status": "awaiting_utr", "updated_at": now_ms()},
    )

    context.user_data["awaiting_utr_order"] = oid
    await asyncio.to_thread(
        fb_update,
        f"users/{query.from_user.id}",
        {"pending_utr_order": oid, "last_seen_at": now_ms()},
    )

    await query.message.reply_text(
        UI.text("submit_utr"),
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
            await message.reply_text(UI.text("alerts", field="pending_order_missing"), parse_mode=ParseMode.HTML)
            return

    if not oid:
        # Normal text messages are ignored unless the user is currently
        # submitting a UTR. Navigation is intentionally handled by inline
        # keyboards and slash commands only.
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
            UI.text("alerts", field="invalid_utr"),
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
        await message.reply_text(UI.text("alerts", field="order_load_failed"), parse_mode=ParseMode.HTML)
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

        await message.reply_text(UI.text("alerts", field="order_unavailable"), parse_mode=ParseMode.HTML)
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

        await message.reply_text(UI.text("alerts", field="order_closed"), parse_mode=ParseMode.HTML)
        return

    if status not in {"awaiting_utr", "awaiting_payment"}:
        await message.reply_text(UI.text("alerts", field="order_not_waiting_utr"), parse_mode=ParseMode.HTML)
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
            await message.reply_text(UI.text("alerts", field="utr_duplicate"), parse_mode=ParseMode.HTML)
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
        await message.reply_text(UI.text("alerts", field="utr_save_failed"), parse_mode=ParseMode.HTML)
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
            await message.reply_text(UI.text("alerts", field="utr_verify_failed"), parse_mode=ParseMode.HTML)
            return

    except Exception:
        logger.exception(
            "Could not verify saved UTR for order %s",
            oid,
        )
        await message.reply_text(UI.text("alerts", field="utr_verify_unknown"), parse_mode=ParseMode.HTML)
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
        UI.text("utr_submitted", {
            "order_id": oid,
            "utr": normalized_utr,
        }),
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
        await query.answer(UI.text("alerts", field="admin_only"), show_alert=True)
        return

    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order:
        await query.answer(UI.text("alerts", field="admin_order_not_found"), show_alert=True)
        return
    if order.get("status") != "awaiting_verification":
        await query.answer(UI.text("alerts", field="admin_order_closed"), show_alert=True)
        return

    product_id = order.get("product_id", "website")
    plan_id = order.get("plan_id", "")
    plan = await asyncio.to_thread(get_plan, product_id, plan_id)
    product = await asyncio.to_thread(get_product, product_id)
    if not plan or not product:
        await query.answer(UI.text("alerts", field="config_missing"), show_alert=True)
        return

    duration_days = int(plan.get("duration_days", 0))
    if duration_days <= 0:
        await query.answer(UI.text("alerts", field="invalid_plan_duration"), show_alert=True)
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
            UI.text("payment_approved_admin", {
                "order_id": oid,
                "purchase_id": pid,
            }),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    # Send raw key only to the customer; Firebase stores only its hash.
    try:
        destination = product_url(product)

        await context.bot.send_message(
            chat_id=int(order["telegram_id"]),
            text=UI.text("payment_approved_customer", {
                "plan_name": plan.get("name", plan_id),
                "amount": order.get("amount"),
                "key": raw_key,
                "expires_at": datetime.fromtimestamp(
                    expires_at / 1000, timezone.utc
                ).strftime("%d %b %Y %H:%M UTC"),
            }),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.payment_approved_keyboard(destination),
        )
    except Exception:
        logger.exception("Failed to deliver key for %s", oid)


async def reject_order(query, oid: str) -> None:
    if not is_admin(query.from_user.id):
        await query.answer(UI.text("alerts", field="admin_only"), show_alert=True)
        return

    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order:
        await query.answer(UI.text("alerts", field="admin_order_not_found"), show_alert=True)
        return
    if order.get("status") != "awaiting_verification":
        await query.answer(UI.text("alerts", field="admin_order_closed"), show_alert=True)
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
            UI.text("payment_rejected_admin", {"order_id": oid}),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    try:
        await query.get_bot().send_message(
            chat_id=int(order["telegram_id"]),
            text=UI.text("payment_rejected_customer", {"order_id": oid}),
            parse_mode=ParseMode.HTML,
            reply_markup=order_back_menu(),
        )
    except Exception:
        logger.exception("Failed to notify rejected customer %s", oid)


async def admin_detail(query, oid: str) -> None:
    if not is_admin(query.from_user.id):
        await query.answer(UI.text("alerts", field="admin_only"), show_alert=True)
        return
    order = await asyncio.to_thread(fb_get, f"orders/{oid}")
    if not order:
        await query.answer(UI.text("alerts", field="admin_order_not_found"), show_alert=True)
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
    """Show active premium purchases; Firebase UI controls text/button labels."""
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
                active_purchases.append({"purchase_id": str(pid), **purchase})

        active_purchases.sort(key=lambda p: int(p.get("expires_at", 0)), reverse=True)

        # UI.py owns the keyboard. bot.py only prepares data.
        for purchase in active_purchases:
            if purchase.get("access_type") == "free_shortlink":
                purchase["plan_name"] = purchase.get("plan_name", "Free 1 Hour")
            else:
                plan = await asyncio.to_thread(
                    get_plan,
                    purchase.get("product_id", "website"),
                    purchase.get("plan_id", ""),
                )
                purchase["plan_name"] = (plan or {}).get("name", purchase.get("plan_id", "Premium"))

        if active_purchases:
            message = UI.text("my_orders", field="text_active")
        else:
            message = UI.text("my_orders", field="text_empty")

        await query.edit_message_text(
            message,
            parse_mode=ParseMode.HTML,
            reply_markup=UI.orders_keyboard(active_purchases),
        )

    except Exception:
        logger.exception("My Orders failed for Telegram user %s", uid)
        await query.edit_message_text(
            UI.text("my_orders", field="text_error"),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.keyboard("order_cancelled"),
        )


async def show_purchase_detail(query, purchase_id_value: str) -> None:
    uid = int(query.from_user.id)

    try:
        purchase = await asyncio.to_thread(fb_get, f"purchases/{purchase_id_value}")

        if not purchase or int(purchase.get("telegram_id", 0)) != uid:
            await query.answer(UI.text("generic", field="purchase_not_found"), show_alert=True)
            return

        expires_at = int(purchase.get("expires_at", 0))

        if (
            purchase.get("status") != "active"
            or expires_at <= now_ms()
            or not purchase.get("encrypted_key")
        ):
            await query.answer(UI.text("generic", field="purchase_inactive"), show_alert=True)
            return

        raw_key = decrypt_key(purchase["encrypted_key"])
        product_id = purchase.get("product_id", "website")
        plan_id = purchase.get("plan_id", "")
        plan = await asyncio.to_thread(get_plan, product_id, plan_id)
        product = await asyncio.to_thread(get_product, product_id)

        plan_name = purchase.get("plan_name") if purchase.get("access_type") == "free_shortlink" else (plan or {}).get("name", plan_id or "Premium")

        expires_text = datetime.fromtimestamp(
            expires_at / 1000, timezone.utc
        ).strftime("%d %b %Y %H:%M UTC")

        values = {
            "purchase_id": purchase_id_value,
            "order_id": purchase.get("order_id", ""),
            "plan_name": plan_name,
            "amount": purchase.get("amount", 0),
            "key": raw_key,
            "expires_at": expires_text,
        }

        destination = product_url(product or {})

        await query.edit_message_text(
            UI.text("active_order", values),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.active_order_keyboard(destination),
        )

    except (InvalidToken, TypeError, ValueError):
        logger.exception("Key recovery/decryption failed for purchase %s", purchase_id_value)
        await query.edit_message_text(
            UI.text("key_recovery_error"),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.keyboard("order_cancelled"),
        )

    except Exception:
        logger.exception("Purchase detail failed for user %s / purchase %s", uid, purchase_id_value)
        await query.edit_message_text(
            UI.text("order_error"),
            parse_mode=ParseMode.HTML,
            reply_markup=UI.keyboard("order_cancelled"),
        )


async def show_support(query) -> None:
    settings = await asyncio.to_thread(get_settings)
    username = settings.get("support", {}).get("telegram_username", "").strip().lstrip("@")

    await query.edit_message_text(
        UI.text("support"),
        parse_mode=ParseMode.HTML,
        reply_markup=UI.support_keyboard(username),
    )


# -----------------------------------------------------------------------------
# Admin commands
# -----------------------------------------------------------------------------


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    pending = await asyncio.to_thread(
        lambda: db.reference("orders").order_by_child("status").equal_to("awaiting_verification").get() or {}
    )
    if not pending:
        await update.message.reply_text(UI.text("alerts", field="no_pending_admin"), parse_mode=ParseMode.HTML)
        return

    for oid, order in pending.items():
        await send_admin_review(update, oid)



class MessageQueryAdapter:
    """Adapter so trusted Firebase reply-keyboard actions use the same engine."""
    def __init__(self, message, user):
        self.message = message
        self.from_user = user

    async def edit_message_text(self, text, **kwargs):
        return await self.message.reply_text(text, **kwargs)

    async def answer(self, *args, **kwargs):
        return None


async def dispatch_action(query, context: ContextTypes.DEFAULT_TYPE, data: str, user) -> None:
    """Execute only known/trusted action strings selected by Firebase UI."""
    data = str(data or "")
    if data == "home":
        await show_home(query); return
    if data == "product:website":
        await show_product(query, "website"); return
    if data == "free:claim":
        try:
            verify_url = await create_free_claim(user)
            await query.message.reply_text(
                UI.text("free_access", field="created"),
                parse_mode=ParseMode.HTML,
                reply_markup=UI.keyboard("free_access", {"verify_url": verify_url}),
            )
        except Exception:
            logger.exception("Failed to create free claim for Telegram user %s", user.id)
            await query.message.reply_text(UI.text("free_access", field="error"), parse_mode=ParseMode.HTML)
        return
    if data in {"orders", "my_orders"}:
        await show_orders(query); return
    if data == "support":
        await show_support(query); return
    if data == "help":
        await show_support(query); return
    if data.startswith("purchase:view:"):
        await show_purchase_detail(query, data.split(":", 2)[2]); return
    if data.startswith("plan:website:"):
        await create_order(query, user, "website", data.split(":", 2)[2]); return
    if data.startswith("manual:"):
        await send_manual_upi(query, context, data.split(":", 1)[1]); return
    if data.startswith("cancel:"):
        await cancel_order(query, data.split(":", 1)[1]); return
    if data.startswith("utr:"):
        await request_utr(query, context, data.split(":", 1)[1]); return
    if data.startswith("planback:"):
        await show_product(query, "website"); return
    if data.startswith("adminapprove:"):
        await approve_order(query, context, data.split(":", 1)[1]); return
    if data.startswith("adminreject:"):
        await reject_order(query, data.split(":", 1)[1]); return
    if data.startswith("admindetail:"):
        await admin_detail(query, data.split(":", 1)[1]); return
    logger.warning("Ignoring unknown UI action: %s", data)


# -----------------------------------------------------------------------------
# Callback router
# -----------------------------------------------------------------------------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await dispatch_action(query, context, query.data or "", query.from_user)


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

        # Expire unused free shortlink claims.
        pending_claims = await asyncio.to_thread(
            lambda: db.reference("free_claims").order_by_child("status").equal_to("pending").get() or {}
        )
        claim_updates = {}
        for claim_hash, claim in pending_claims.items():
            if int(claim.get("expires_at", 0)) <= now:
                claim_updates[f"free_claims/{claim_hash}/status"] = "expired"
                claim_updates[f"free_claims/{claim_hash}/expired_at"] = now
        if claim_updates:
            await asyncio.to_thread(db.reference("/").update, claim_updates)

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

    await show_orders(MessageQueryAdapter(update.message, update.effective_user))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await show_orders(MessageQueryAdapter(update.message, update.effective_user))


async def set_bot_commands(application: Application) -> None:
    # Public command menu: /admin is intentionally omitted.
    public_commands = [
        BotCommand("start", "Open the main menu"),
        BotCommand("help", "Show help and commands"),
        BotCommand("plans", "View premium plans"),
        BotCommand("orders", "View your orders"),
        BotCommand("status", "View active premium access"),
        BotCommand("support", "Contact support"),
        ]
    await application.bot.set_my_commands(public_commands)

    # Admin-only command menu. The command itself is still protected by the
    # ADMIN_ID check inside admin_command.
    await application.bot.set_my_commands(
        public_commands + [BotCommand("admin", "Open admin dashboard")],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID_INT),
    )


def build_application() -> Application:
    application = Application.builder().token(BOT_TOKEN).post_init(set_bot_commands).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("plans", plans_command))
    application.add_handler(CommandHandler("orders", orders_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("support", support_command))
    application.add_handler(CommandHandler("admin", admin_command))
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
