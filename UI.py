"""
SILENT PREMIUM — Firebase UI Engine

UI is stored in Firebase Realtime Database under /ui.

This module is presentation-only:
- HTML text comes from Firebase.
- Inline keyboards come from Firebase.
- Persistent reply keyboards come from Firebase.
- {{variables}} are substituted at runtime.
- Firebase can select only trusted action strings; it never executes Python.

For compatibility, both `inline_keyboard.rows` and the older `buttons` format
are accepted for inline keyboards.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from firebase_admin import db
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.constants import ParseMode

UI_ROOT = "ui"
_TOKEN = re.compile(r"{{\s*([a-zA-Z0-9_.-]+)\s*}}")

DEFAULT_UI: dict[str, dict[str, Any]] = {
    "welcome": {
        "enabled": True,
        "html": "<b>👑 SILENT PREMIUM</b>\n\nPremium access made simple.\n\nChoose a service below:",
        "inline_keyboard": {
            "enabled": True,
            "rows": [
                [{"text": "🌐 Website Premium", "action": "product:website", "style": "success"}],
                [
                    {"text": "📦 My Orders", "action": "orders", "style": "primary"},
                    {"text": "📞 Support / Help", "action": "support", "style": "primary"},
                ],
            ],
        },
        "reply_prompt": "Choose an option from the menu below.",
        "reply_keyboard": {
            "enabled": True,
            "resize_keyboard": True,
            "persistent": True,
            "rows": [
                [
                    {"text": "💎 Plans", "action": "product:website"},
                    {"text": "📊 My Status", "action": "orders"},
                ],
                [
                    {"text": "❓ Help", "action": "help"},
                    {"text": "📞 Support", "action": "support"},
                ],
            ],
        },
    },
    "product": {
        "enabled": True,
        "html": "<b>👑 {{product_name}}</b>\n\n{{description}}\n\n<b>Choose your plan:</b>",
        "inline_keyboard": {"enabled": True, "rows": []},
    },
    "plans": {
        "enabled": True,
        "html": "<b>📦 {{product_name}}</b>\n\n<b>Choose your plan:</b>",
        "plan_button": "🟢 {{plan_name}} — ₹{{price}}",
        "inline_keyboard": {"enabled": True, "rows": []},
        "back_button": "◀️ Back",
    },
    "payment_methods": {
        "enabled": True,
        "html": "<b>💳 CHOOSE YOUR PAYMENT METHOD</b>\n\nOrder: <code>{{order_id}}</code>\nPlan: <b>{{plan_name}}</b>\nAmount: <b>₹{{amount}}</b>\n\nSelect your preferred payment method:",
        "inline_keyboard": {
            "enabled": True,
            "rows": [
                [{"text": "🧾 Manual UPI", "action": "manual:{{order_id}}", "style": "success"}],
                [{"text": "◀️ Back to Plans", "action": "planback:{{order_id}}", "style": "primary"}],
            ],
        },
    },
    "manual_upi": {
        "enabled": True,
        "caption": "<b>🧾 MANUAL UPI PAYMENT</b>\n\nOrder: <code>{{order_id}}</code>\nAmount: <b>₹{{amount}}</b>\nUPI ID: <code>{{upi_id}}</code>\nName: {{upi_name}}\n\n<b>Instructions</b>\n1. Pay the exact amount.\n2. Use <code>{{order_id}}</code> as the payment note if supported.\n3. Submit your UTR after payment.\n\n⏳ Payment window: {{payment_window_minutes}} minutes",
    },
    "payment_pending": {
        "enabled": True,
        "html": "<b>⏳ PAYMENT PENDING</b>\n\nOrder: <code>{{order_id}}</code>\nAmount: <b>₹{{amount}}</b>\n\nComplete the payment and submit your UTR.",
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
        "back_button": "◀️ Back to Main Menu",
        "inline_keyboard": {"enabled": True, "rows": []},
    },
    "active_order": {
        "enabled": True,
        "html": "<b>🔑 ACTIVE PREMIUM ORDER</b>\n\nPurchase: <code>{{purchase_id}}</code>\nOrder: <code>{{order_id}}</code>\nPlan: <b>{{plan_name}}</b>\nPaid: <b>₹{{amount}}</b>\n\n<b>🔑 Your Premium Key</b>\n\n<code>{{key}}</code>\n\n⏰ Expires: <code>{{expires_at}}</code>\n\n<i>You can open this order again anytime from My Orders while it remains active.</i>",
        "product_button": "🌐 Open Product",
        "back_button": "📦 Back to My Orders",
        "inline_keyboard": {"enabled": True, "rows": []},
    },
    "support": {
        "enabled": True,
        "html": "<b>📞 SUPPORT / HELP</b>\n\nFor payment or premium-access issues, contact support.",
        "contact_button": "📞 Contact Support",
        "back_button": "◀️ Back to Main Menu",
        "inline_keyboard": {"enabled": True, "rows": []},
    },
    "payment_approved_customer": {
        "enabled": True,
        "html": "<b>🎉 PAYMENT APPROVED</b>\n\nYour premium access is now active.\n\n📦 Plan: <b>{{plan_name}}</b>\n💰 Paid: ₹{{amount}}\n\n🔑 <b>Your Premium Key</b>\n\n<code>{{key}}</code>\n\n⏰ Expires: <code>{{expires_at}}</code>",
        "product_button": "🌐 Open Product",
        "orders_button": "📦 My Orders",
    },
    "payment_approved_admin": {"enabled": True, "html": "<b>✅ PAYMENT APPROVED</b>\n\nOrder: <code>{{order_id}}</code>\nPurchase: <code>{{purchase_id}}</code>\nKey issued to the customer.\n\nStatus: Active"},
    "payment_rejected_customer": {"enabled": True, "html": "<b>❌ PAYMENT NOT APPROVED</b>\n\nOrder: <code>{{order_id}}</code>\n\nYour payment could not be verified.\nPlease contact support if you believe this was a mistake."},
    "payment_rejected_admin": {"enabled": True, "html": "<b>❌ PAYMENT REJECTED</b>\n\nOrder: <code>{{order_id}}</code> has been rejected."},
    "key_recovery_error": {"enabled": True, "html": "<b>⚠️ KEY RECOVERY ERROR</b>\n\nYour premium purchase exists, but the key could not be recovered.\n\nPlease contact support."},
    "order_error": {"enabled": True, "html": "<b>⚠️ ORDER ERROR</b>\n\nI couldn't open this premium order right now.\n\nPlease try again."},
    "key_delivery": {"enabled": True, "html": "<b>🔑 YOUR PREMIUM KEY</b>\n\n<code>{{key}}</code>\n\n⏰ Expires: <code>{{expires_at}}</code>", "product_button": "🌐 Open Product", "orders_button": "📦 My Orders"},
    "admin_review": {"enabled": True, "inline_keyboard": {"enabled": True, "rows": [[{"text": "✅ Approve", "action": "approve"}, {"text": "❌ Reject", "action": "reject"}], [{"text": "🔎 Order Details", "action": "detail"}]]}},
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
    # New schema uses `html`; older V8 schema used `text`.
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
    # Backward compatibility with V8's `buttons`.
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
            built: list[InlineKeyboardButton] = []
            for item in row:
                if isinstance(item, dict):
                    button = _button(item, values)
                    if button:
                        built.append(button)
            if built:
                rows.append(built)

    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(rows)


def button(state: str, field: str, values: dict[str, Any], callback_data: str, style: Optional[str] = None) -> InlineKeyboardButton:
    data = get_state(state)
    label = str(render(data.get(field, "Button"), values))
    kwargs: dict[str, Any] = {"text": label, "callback_data": callback_data}
    if style in {"primary", "success", "danger"}:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def reply_keyboard(state: str = "welcome", values: Optional[dict[str, Any]] = None) -> Optional[ReplyKeyboardMarkup]:
    values = values or {}
    data = get_state(state)
    config = data.get("reply_keyboard")
    if not isinstance(config, dict) or config.get("enabled", False) is False:
        return None

    rows: list[list[KeyboardButton]] = []
    for row in config.get("rows", []):
        built: list[KeyboardButton] = []
        if not isinstance(row, list):
            continue
        for item in row:
            if isinstance(item, str):
                built.append(KeyboardButton(str(render(item, values))))
                continue
            if not isinstance(item, dict) or item.get("enabled", True) is False:
                continue
            label = str(render(item.get("text", "Button"), values))
            built.append(KeyboardButton(label))
        if built:
            rows.append(built)

    if not rows:
        return None

    kwargs: dict[str, Any] = {
        "keyboard": rows,
        "resize_keyboard": bool(config.get("resize_keyboard", True)),
        "one_time_keyboard": bool(config.get("one_time_keyboard", False)),
    }
    if "selective" in config:
        kwargs["selective"] = bool(config.get("selective"))
    if "input_field_placeholder" in config:
        kwargs["input_field_placeholder"] = str(render(config.get("input_field_placeholder"), values))
    # PTB 22.x supports persistent reply keyboards.
    if "persistent" in config:
        kwargs["is_persistent"] = bool(config.get("persistent"))
    return ReplyKeyboardMarkup(**kwargs)


def reply_action(label: str, state: str = "welcome") -> Optional[str]:
    """Map a persistent reply-keyboard label to its trusted action."""
    data = get_state(state)
    config = data.get("reply_keyboard")
    if not isinstance(config, dict) or config.get("enabled", False) is False:
        return None
    wanted = str(label).strip()
    for row in config.get("rows", []):
        if not isinstance(row, list):
            continue
        for item in row:
            if isinstance(item, str):
                if item.strip() == wanted:
                    return item.strip()
            elif isinstance(item, dict):
                rendered = str(render(item.get("text", ""), {})).strip()
                if rendered == wanted:
                    return str(item.get("action", "")).strip() or None
    return None


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
    reply_keyboard = staticmethod(reply_keyboard)
    reply_action = staticmethod(reply_action)
    parse_mode = ParseMode.HTML
