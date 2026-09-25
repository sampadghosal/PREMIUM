"""
SILENT PREMIUM — Firebase UI Engine

Single source of truth for all customer-facing Telegram UI.

Rules:
- bot.py contains business logic/actions only.
- UI.py builds every customer-facing inline keyboard.
- Firebase /ui controls copy, labels, button styles and static layouts.
- Dynamic plan/order buttons are generated here from Firebase data, so bot.py
  never creates a second keyboard for the same screen.
- Customer messages use inline keyboards only.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from firebase_admin import db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

UI_ROOT = "ui"
_TOKEN = re.compile(r"{{\s*([a-zA-Z0-9_.-]+)\s*}}")

DEFAULT_UI: dict[str, dict[str, Any]] = {
    "welcome": {
        "enabled": True,
        "html": (
            "<b>👑 SILENT PREMIUM STORE</b>\n\n"
            "<blockquote>Welcome to the official premium store!\n"
            "Upgrade your membership to get instant access to premium website content and exclusive features.</blockquote>\n\n"
            "💎 <b>Available Premium Services:</b>\n\n"
            "👑 FabHouse Premium — Premium Website Access\n\n"
            "<i>Select a service below or use /plans to view complete pricing.</i>\n\n"
            "⚡ Powered by @SILENT_MOD_SG"
        ),
        "inline_keyboard": {
            "enabled": True,
            "rows": [
                [{"text": "👑 FabHouse Premium", "action": "product:website", "style": "success"}],
                [
                    {"text": "📦 My Orders", "action": "orders", "style": "primary"},
                    {"text": "📞 Support / Help", "action": "support", "style": "primary"},
                ],
            ],
        },
    },
    "help": {
        "enabled": True,
        "html": (
            "<b>❓ SILENT PREMIUM — HELP</b>\n\n"
            "Use the commands below or the buttons in the message.\n\n"
            "<b>Available commands</b>\n"
            "/start — Open the main menu\n"
            "/help — Show this help\n"
            "/plans — View premium plans\n"
            "/orders — View your orders\n"
            "/status — View active premium access\n"
            "/support — Contact support\n\n"
            "<i>/admin is available only to the configured admin account.</i>"
        ),
        "inline_keyboard": {
            "enabled": True,
            "rows": [
                [{"text": "👑 FabHouse Premium", "action": "product:website", "style": "success"}],
                [
                    {"text": "📦 My Orders", "action": "orders", "style": "primary"},
                    {"text": "📞 Support", "action": "support", "style": "primary"},
                ],
                [{"text": "🏠 Main Menu", "action": "home", "style": "primary"}],
            ],
        },
    },
    "product": {
        "enabled": True,
        "html": (
            "<b>👑 {{product_name}}</b>\n\n"
            "{{description}}\n\n"
            "<b>Choose your plan:</b>"
        ),
        "inline_keyboard": {"enabled": True, "rows": []},
    },
    "plans": {
        "enabled": True,
        "html": (
            "<b>🔥 FABHOUSE PREMIUM</b>\n\n"
            "<blockquote>✨ <b>Special Premium Offer!</b> ✨\n"
            "Get premium website access with instant activation and full access to the available premium features.</blockquote>\n\n"
            "💎 <b>What You Get:</b>\n\n"
            "<blockquote>• Premium Website Access\n"
            "• Individual Premium License Key\n"
            "• Clean &amp; Ad-Free Experience</blockquote>\n\n"
            "<i>Select your preferred duration below:</i>\n\n"
            "⚡ Powered by @SILENT_MOD_SG"
        ),
        "plan_button": "👑 {{plan_name}} (₹{{price}})",
        "plan_style": "success",
        "back_button": "◀️ Back to Main Menu",
        "back_action": "home",
        "inline_keyboard": {"enabled": True, "rows": []},
    },
    "payment_methods": {
        "enabled": True,
        "html": (
            "<b>💳 CHECKOUT: {{plan_name}}</b>\n\n"
            "<blockquote>💎 Plan: {{plan_name}}\n"
            "💰 Price: ₹{{amount}}\n"
            "⚡ Delivery: Instant Activation</blockquote>\n\n"
            "<b>💳 Choose Your Payment Method:</b>"
        ),
        "inline_keyboard": {
            "enabled": True,
            "rows": [
                [{"text": "🧾 Manual UPI (UTR Proof)", "action": "manual:{{order_id}}", "style": "success"}],
                [{"text": "◀️ Back to Plans", "action": "planback:{{order_id}}", "style": "primary"}],
            ],
        },
    },
    "manual_upi": {
        "enabled": True,
        "caption": (
            "<b>🧾 MANUAL UPI PAYMENT</b>\n\n"
            "Order: <code>{{order_id}}</code>\n"
            "Amount: <b>₹{{amount}}</b>\n"
            "UPI ID: <code>{{upi_id}}</code>\n"
            "Name: {{upi_name}}\n\n"
            "<b>Instructions</b>\n"
            "1. Pay the exact amount.\n"
            "2. Use <code>{{order_id}}</code> as the payment note if supported.\n"
            "3. Submit your UTR after payment.\n\n"
            "⏳ Payment window: {{payment_window_minutes}} minutes"
        ),
    },
    "payment_pending": {
        "enabled": True,
        "html": (
            "<b>⏳ PAYMENT PENDING</b>\n\n"
            "Order: <code>{{order_id}}</code>\n"
            "Amount: <b>₹{{amount}}</b>\n\n"
            "Complete the payment and submit your UTR."
        ),
        "inline_keyboard": {
            "enabled": True,
            "rows": [
                [{"text": "🧾 Submit UTR", "action": "utr:{{order_id}}", "style": "success"}],
                [{"text": "🔴 Cancel Order", "action": "cancel:{{order_id}}", "style": "danger"}],
            ],
        },
    },
    "submit_utr": {
        "enabled": True,
        "html": "<b>🧾 SUBMIT UTR</b>\n\nPlease send your UTR/reference number.\n\nExample:\n<code>123456789012</code>",
    },
    "utr_submitted": {
        "enabled": True,
        "html": "<b>✅ UTR SUBMITTED</b>\n\nOrder: <code>{{order_id}}</code>\nUTR: <code>{{utr}}</code>\n\nYour payment is now waiting for manual verification.",
    },
    "order_cancelled": {
        "enabled": True,
        "html": "<b>🔴 ORDER CANCELLED</b>\n\nOrder <code>{{order_id}}</code> has been cancelled.",
        "inline_keyboard": {"enabled": True, "rows": [[{"text": "◀️ Back to Main Menu", "action": "home", "style": "primary"}]]},
    },
    "order_expired": {
        "enabled": True,
        "html": "<b>⏰ ORDER EXPIRED</b>\n\nThe payment window has expired. Please create a new order.",
        "inline_keyboard": {"enabled": True, "rows": [[{"text": "◀️ Back to Main Menu", "action": "home", "style": "primary"}]]},
    },
    "my_orders": {
        "enabled": True,
        "text_active": "<b>📦 MY ORDERS</b>\n\n<b>🟢 ACTIVE PREMIUM</b>\n\nTap an active order below to view your premium key.",
        "text_empty": "<b>📦 MY ORDERS</b>\n\nYou currently have no active premium orders.",
        "text_error": "<b>⚠️ MY ORDERS</b>\n\nI couldn't load your active orders right now.\n\nPlease try again.",
        "active_button": "🟢 {{plan_name}} · ₹{{amount}}",
        "active_style": "success",
        "back_button": "◀️ Back to Main Menu",
        "inline_keyboard": {"enabled": True, "rows": []},
    },
    "active_order": {
        "enabled": True,
        "html": "<b>🔑 ACTIVE PREMIUM ORDER</b>\n\nPurchase: <code>{{purchase_id}}</code>\nOrder: <code>{{order_id}}</code>\nPlan: <b>{{plan_name}}</b>\nPaid: <b>₹{{amount}}</b>\n\n<b>🔑 Your Premium Key</b>\n\n<code>{{key}}</code>\n\n⏰ Expires: <code>{{expires_at}}</code>",
        "product_button": "🌐 Open Product",
        "back_button": "📦 Back to My Orders",
        "back_style": "primary",
        "inline_keyboard": {"enabled": True, "rows": []},
    },
    "support": {
        "enabled": True,
        "html": "<b>📞 SUPPORT / HELP</b>\n\nFor payment or premium-access issues, contact support.",
        "contact_button": "📞 Contact Support",
        "back_button": "◀️ Back to Main Menu",
        "back_style": "primary",
        "inline_keyboard": {"enabled": True, "rows": []},
    },
    "payment_approved_customer": {
        "enabled": True,
        "html": "<b>🎉 PAYMENT APPROVED</b>\n\nYour premium access is now active.\n\n📦 Plan: <b>{{plan_name}}</b>\n💰 Paid: ₹{{amount}}\n\n🔑 <b>Your Premium Key</b>\n\n<code>{{key}}</code>\n\n⏰ Expires: <code>{{expires_at}}</code>",
        "product_button": "🌐 Open Product",
        "orders_button": "📦 My Orders",
        "orders_action": "my_orders",
        "orders_style": "primary",
    },
    "payment_approved_admin": {
        "enabled": True,
        "html": "<b>✅ PAYMENT APPROVED</b>\n\nOrder: <code>{{order_id}}</code>\nPurchase: <code>{{purchase_id}}</code>\nKey issued to the customer.\n\nStatus: Active"
    },
    "payment_rejected_customer": {
        "enabled": True,
        "html": "<b>❌ PAYMENT NOT APPROVED</b>\n\nOrder: <code>{{order_id}}</code>\n\nYour payment could not be verified.\nPlease contact support if you believe this was a mistake."
    },
    "payment_rejected_admin": {
        "enabled": True,
        "html": "<b>❌ PAYMENT REJECTED</b>\n\nOrder: <code>{{order_id}}</code> has been rejected."
    },
    "key_recovery_error": {
        "enabled": True,
        "html": "<b>⚠️ KEY RECOVERY ERROR</b>\n\nYour premium purchase exists, but the key could not be recovered.\n\nPlease contact support."
    },
    "order_error": {
        "enabled": True,
        "html": "<b>⚠️ ORDER ERROR</b>\n\nI couldn't open this premium order right now.\n\nPlease try again."
    },
    "admin_review": {
        "enabled": True,
        "inline_keyboard": {
            "enabled": True,
            "rows": [
                [
                    {"text": "✅ Approve", "action": "approve"},
                    {"text": "❌ Reject", "action": "reject"}
                ],
                [{"text": "🔎 Order Details", "action": "detail"}]
            ]
        }
    },
    "alerts": {
        "payment_instructions_sent": "Payment instructions sent.",
        "plan_unavailable": "<b>⚠️ PLAN UNAVAILABLE</b>\n\nPlease choose another plan.",
        "pending_order_missing": "⚠️ I couldn't find your pending order right now. Please press Submit UTR again.",
        "invalid_utr": "❌ Please send a valid UTR/reference number.\n\nExample: <code>123456789012</code>",
        "order_load_failed": "⚠️ I couldn't load your order right now. Please try again.",
        "order_unavailable": "That order is no longer available.",
        "order_closed": "This order is no longer active.",
        "order_not_waiting_utr": "This order is not currently waiting for a UTR. Please use Submit UTR again.",
        "utr_duplicate": "⚠️ This UTR has already been submitted for another order.\nPlease check the UTR and try again.",
        "utr_save_failed": "⚠️ I couldn't save the UTR right now. Please try again in a moment.",
        "utr_verify_failed": "⚠️ The UTR could not be confirmed as saved. Please try again.",
        "utr_verify_unknown": "⚠️ The UTR was submitted, but I couldn't confirm the save yet. Please do not submit a different UTR; contact support if the order does not update.",
        "admin_only": "Admin only.",
        "admin_order_not_found": "Order not found.",
        "admin_order_closed": "This order is no longer awaiting verification.",
        "config_missing": "Product/plan configuration missing.",
        "invalid_plan_duration": "Invalid plan duration.",
        "no_pending_admin": "<b>📋 No pending payment verifications.</b>"
    },
    "generic": {
        "back_button": "◀️ Back to Main Menu",
        "order_not_found": "Order not found.",
        "order_closed": "This order is no longer active.",
        "order_already_closed": "This order is already closed.",
        "upi_missing": "UPI is not configured yet. Contact admin.",
        "payment_window_expired": "Payment window expired. Create a new order.",
        "purchase_not_found": "Purchase not found.",
        "purchase_inactive": "This premium order is no longer active.",
        "admin_only": "Admin only."
    }
}


def _deep_merge(default: Any, remote: Any) -> Any:
    if isinstance(default, dict) and isinstance(remote, dict):
        result = dict(default)
        for key, value in remote.items():
            result[key] = _deep_merge(result[key], value) if key in result else value
        return result
    return remote if remote is not None else default


def get_state(state: str) -> dict[str, Any]:
    try:
        remote = db.reference(f"{UI_ROOT}/{state}").get()
    except Exception:
        remote = None
    return _deep_merge(DEFAULT_UI.get(state, {}), remote or {})


def render(template: Any, values: Optional[dict[str, Any]] = None) -> Any:
    if not isinstance(template, str):
        return template
    values = values or {}

    def replace(match: re.Match[str]) -> str:
        value: Any = values
        for part in match.group(1).split("."):
            if isinstance(value, dict):
                value = value.get(part, "")
            else:
                return ""
        return str(value if value is not None else "")

    return _TOKEN.sub(replace, template)


def text(state: str, values: Optional[dict[str, Any]] = None, field: str = "html") -> str:
    data = get_state(state)
    template = data.get(field)
    if template is None and field == "html":
        template = data.get("text", "")
    return str(render(template or "", values))


def _button(config: dict[str, Any], values: dict[str, Any]) -> Optional[InlineKeyboardButton]:
    if config.get("enabled", True) is False:
        return None
    label = str(render(config.get("text", "Button"), values))
    url = render(config.get("url"), values) if config.get("url") else None
    if url:
        return InlineKeyboardButton(text=label, url=str(url))
    action = render(config.get("action", config.get("callback", "")), values)
    if not action:
        return None
    kwargs: dict[str, Any] = {"text": label, "callback_data": str(action)}
    style = config.get("style")
    if style in {"primary", "success", "danger"}:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def _inline_rows(data: dict[str, Any]) -> list[list[dict[str, Any]]]:
    inline = data.get("inline_keyboard")
    if isinstance(inline, dict):
        rows = inline.get("rows", [])
        if isinstance(rows, list):
            return rows
    rows = data.get("buttons", [])
    return rows if isinstance(rows, list) else []


def keyboard(state: str, values: Optional[dict[str, Any]] = None, extra_rows: Optional[list[list[InlineKeyboardButton]]] = None) -> InlineKeyboardMarkup:
    values = values or {}
    data = get_state(state)
    inline = data.get("inline_keyboard", {})
    enabled = inline.get("enabled", True) if isinstance(inline, dict) else True
    rows: list[list[InlineKeyboardButton]] = []

    if enabled:
        for row in _inline_rows(data):
            if not isinstance(row, list):
                continue
            built = []
            for item in row:
                if isinstance(item, dict):
                    btn = _button(item, values)
                    if btn:
                        built.append(btn)
            if built:
                rows.append(built)

    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(rows)


def button(state: str, field: str, values: dict[str, Any], callback_data: str, style: Optional[str] = None) -> InlineKeyboardButton:
    data = get_state(state)
    label = str(render(data.get(field, "Button"), values))
    kwargs: dict[str, Any] = {"text": label, "callback_data": callback_data}
    chosen_style = style or data.get(f"{field}_style") or data.get("style")
    if chosen_style in {"primary", "success", "danger"}:
        kwargs["style"] = chosen_style
    return InlineKeyboardButton(**kwargs)


def plans_keyboard(product: dict[str, Any]) -> InlineKeyboardMarkup:
    """Build exactly ONE plan keyboard from Firebase UI + product plans."""
    state = get_state("plans")
    plan_template = state.get("plan_button", "👑 {{plan_name}} (₹{{price}})")
    plan_style = state.get("plan_style", "success")
    rows: list[list[InlineKeyboardButton]] = []

    plans = product.get("plans", {}) or {}
    for plan_id, plan in plans.items():
        if not isinstance(plan, dict) or not plan.get("enabled", True):
            continue
        values = {
            "plan_id": plan_id,
            "plan_name": plan.get("name", plan_id),
            "price": plan.get("price", 0),
            "product_id": "website",
        }
        kwargs = {
            "text": str(render(plan_template, values)),
            "callback_data": f"plan:website:{plan_id}",
        }
        if plan_style in {"primary", "success", "danger"}:
            kwargs["style"] = plan_style
        rows.append([InlineKeyboardButton(**kwargs)])

    back_text = str(render(state.get("back_button", "◀️ Back to Main Menu"), {}))
    back_action = str(render(state.get("back_action", "home"), {}))
    rows.append([InlineKeyboardButton(text=back_text, callback_data=back_action, style="primary")])
    return InlineKeyboardMarkup(rows)


def orders_keyboard(active_purchases: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    state = get_state("my_orders")
    rows: list[list[InlineKeyboardButton]] = []
    for purchase in active_purchases:
        rows.append([
            button(
                "my_orders",
                "active_button",
                {
                    "plan_name": purchase.get("plan_name", "Premium"),
                    "amount": purchase.get("amount", 0),
                    "purchase_id": purchase.get("purchase_id", ""),
                },
                f"purchase:view:{purchase.get('purchase_id', '')}",
                state.get("active_style", "success"),
            )
        ])
    rows.append([button("my_orders", "back_button", {}, "home", "primary")])
    return InlineKeyboardMarkup(rows)


def active_order_keyboard(destination: str) -> InlineKeyboardMarkup:
    state = get_state("active_order")
    rows: list[list[InlineKeyboardButton]] = []
    if destination and "YOUR-WEBSITE-URL" not in destination:
        rows.append([InlineKeyboardButton(state.get("product_button", "🌐 Open Product"), url=destination)])
    rows.append([button("active_order", "back_button", {}, "my_orders", state.get("back_style", "primary"))])
    return InlineKeyboardMarkup(rows)


def support_keyboard(username: str) -> InlineKeyboardMarkup:
    state = get_state("support")
    rows: list[list[InlineKeyboardButton]] = []
    if username:
        rows.append([InlineKeyboardButton(state.get("contact_button", "📞 Contact Support"), url=f"https://t.me/{username}")])
    rows.append([button("support", "back_button", {}, "home", state.get("back_style", "primary"))])
    return InlineKeyboardMarkup(rows)


def payment_approved_keyboard(destination: str) -> InlineKeyboardMarkup:
    state = get_state("payment_approved_customer")
    rows: list[list[InlineKeyboardButton]] = []
    if destination and "YOUR-WEBSITE-URL" not in destination:
        rows.append([InlineKeyboardButton(state.get("product_button", "🌐 Open Product"), url=destination)])
    rows.append([InlineKeyboardButton(
        state.get("orders_button", "📦 My Orders"),
        callback_data=str(state.get("orders_action", "my_orders")),
        style=state.get("orders_style", "primary"),
    )])
    return InlineKeyboardMarkup(rows)


def admin_review_keyboard(order_id: str) -> InlineKeyboardMarkup:
    state = get_state("admin_review")
    values = {"order_id": order_id}
    rows: list[list[InlineKeyboardButton]] = []
    for row in _inline_rows(state):
        built: list[InlineKeyboardButton] = []
        for item in row:
            if not isinstance(item, dict):
                continue
            action = str(render(item.get("action", ""), values))
            if action == "approve":
                action = f"adminapprove:{order_id}"
            elif action == "reject":
                action = f"adminreject:{order_id}"
            elif action == "detail":
                action = f"admindetail:{order_id}"
            if not action:
                continue
            cfg = dict(item)
            cfg["action"] = action
            btn = _button(cfg, values)
            if btn:
                built.append(btn)
        if built:
            rows.append(built)
    return InlineKeyboardMarkup(rows)


def ui_config() -> dict[str, Any]:
    try:
        return db.reference(UI_ROOT).get() or {}
    except Exception:
        return {}


class UI:
    get_state = staticmethod(get_state)
    render = staticmethod(render)
    text = staticmethod(text)
    keyboard = staticmethod(keyboard)
    button = staticmethod(button)
    plans_keyboard = staticmethod(plans_keyboard)
    orders_keyboard = staticmethod(orders_keyboard)
    active_order_keyboard = staticmethod(active_order_keyboard)
    support_keyboard = staticmethod(support_keyboard)
    payment_approved_keyboard = staticmethod(payment_approved_keyboard)
    admin_review_keyboard = staticmethod(admin_review_keyboard)
    parse_mode = ParseMode.HTML
