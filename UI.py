"""
SILENT PREMIUM - Firebase UI Engine

All user-facing Telegram UI is defined in Firebase Realtime Database under:
    ui/<state>

Python remains the execution engine:
- validates users/orders
- performs payments/key operations
- maps safe action names to existing functions

Firebase controls:
- HTML text
- button labels
- button order/layout
- URLs
- callback action names
- optional visibility/enabled flags

Do NOT put Python code in Firebase.
"""

from __future__ import annotations

import html
import re
from typing import Any, Optional, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from firebase_admin import db

UI_ROOT = "ui"

DEFAULT_UI: dict[str, dict[str, Any]] = {
    "welcome": {
        "text": "<b>👑 SILENT PREMIUM</b>\n\nPremium access made simple.\n\nChoose a service below:",
        "buttons": [
            [{"text": "🌐 Website Premium", "action": "product:website", "style": "success"}],
            [
                {"text": "📦 My Orders", "action": "orders", "style": "primary"},
                {"text": "📞 Support / Help", "action": "support", "style": "primary"},
            ],
        ],
    },
    "product": {
        "text": "<b>👑 {{product_name}}</b>\n\n{{description}}\n\n<b>Choose your plan:</b>",
        "buttons": [
            [{"text": "◀️ Back", "action": "home", "style": "primary"}],
        ],
    },
    "plans": {
        "text": "<b>📦 {{product_name}}</b>\n\n<b>Choose your plan:</b>",
        "plan_button": "🟢 {{plan_name}} — ₹{{price}}",
        "back_button": "◀️ Back",
        "buttons": [],
    },
    "payment_methods": {
        "text": "<b>💳 CHOOSE YOUR PAYMENT METHOD</b>\n\nOrder: <code>{{order_id}}</code>\nPlan: <b>{{plan_name}}</b>\nAmount: <b>₹{{amount}}</b>",
        "buttons": [
            [{"text": "🧾 Manual UPI", "action": "manual:{{order_id}}", "style": "success"}],
            [{"text": "◀️ Back", "action": "product:{{product_id}}", "style": "primary"}],
        ],
    },
    "manual_upi": {
        "caption": "<b>🧾 MANUAL UPI PAYMENT</b>\n\nOrder: <code>{{order_id}}</code>\nAmount: <b>₹{{amount}}</b>\nUPI ID: <code>{{upi_id}}</code>\nName: {{upi_name}}\n\n<b>Instructions</b>\n1. Pay the exact amount.\n2. Use <code>{{order_id}}</code> as the payment note if supported.\n3. Submit your UTR after payment.\n\n⏳ Payment window: {{payment_window_minutes}} minutes",
    },
    "payment_pending": {
        "text": "<b>⏳ PAYMENT PENDING</b>\n\nOrder: <code>{{order_id}}</code>\nAmount: <b>₹{{amount}}</b>\n\nComplete the payment and submit your UTR.",
        "buttons": [
            [{"text": "🧾 Submit UTR", "action": "utr:{{order_id}}", "style": "success"}],
            [{"text": "🔴 Cancel Order", "action": "cancel:{{order_id}}", "style": "danger"}],
        ],
    },
    "submit_utr": {
        "text": "<b>🧾 SUBMIT UTR</b>\n\nPlease send your UTR/reference number.\n\nExample:\n<code>123456789012</code>",
    },
    "utr_submitted": {
        "text": "<b>✅ UTR SUBMITTED</b>\n\nOrder: <code>{{order_id}}</code>\nUTR: <code>{{utr}}</code>\n\nYour payment is now waiting for manual verification.",
    },
    "order_cancelled": {
        "text": "<b>🔴 ORDER CANCELLED</b>\n\nOrder <code>{{order_id}}</code> has been cancelled.",
        "buttons": [[{"text": "◀️ Back to Main Menu", "action": "home", "style": "primary"}]],
    },
    "order_expired": {
        "text": "<b>⏰ ORDER EXPIRED</b>\n\nThe payment window has expired. Please create a new order.",
        "buttons": [[{"text": "◀️ Back to Main Menu", "action": "home", "style": "primary"}]],
    },
    "my_orders": {
        "text_active": "<b>📦 MY ORDERS</b>\n\n<b>🟢 ACTIVE PREMIUM</b>\n\nTap an active order below to view your premium key.",
        "text_empty": "<b>📦 MY ORDERS</b>\n\nYou currently have no active premium orders.",
        "text_error": "<b>⚠️ MY ORDERS</b>\n\nI couldn't load your active orders right now.\n\nPlease try again.",
        "active_button": "🟢 {{plan_name}} · ₹{{amount}}",
        "back_button": "◀️ Back to Main Menu",
    },
    "active_order": {
        "text": "<b>🔑 ACTIVE PREMIUM ORDER</b>\n\nPurchase: <code>{{purchase_id}}</code>\nOrder: <code>{{order_id}}</code>\nPlan: <b>{{plan_name}}</b>\nPaid: <b>₹{{amount}}</b>\n\n<b>🔑 Your Premium Key</b>\n\n<code>{{key}}</code>\n\n⏰ Expires: <code>{{expires_at}}</code>\n\n<i>You can open this order again anytime from My Orders while it remains active.</i>",
        "product_button": "🌐 Open Product",
        "back_button": "📦 Back to My Orders",
    },
    "support": {
        "text": "<b>📞 SUPPORT / HELP</b>\n\nFor payment or premium-access issues, contact support.",
        "contact_button": "📞 Contact Support",
        "back_button": "◀️ Back to Main Menu",
    },
    "payment_approved_customer": {
        "text": "<b>🎉 PAYMENT APPROVED</b>\n\nYour premium access is now active.\n\n📦 Plan: <b>{{plan_name}}</b>\n💰 Paid: ₹{{amount}}\n\n🔑 <b>Your Premium Key</b>\n\n<code>{{key}}</code>\n\n⏰ Expires: <code>{{expires_at}}</code>",
        "product_button": "🌐 Open Product",
        "orders_button": "📦 My Orders",
    },
    "payment_approved_admin": {
        "text": "<b>✅ PAYMENT APPROVED</b>\n\nOrder: <code>{{order_id}}</code>\nPurchase: <code>{{purchase_id}}</code>\nKey issued to the customer.\n\nStatus: Active",
    },
    "payment_rejected_customer": {
        "text": "<b>❌ PAYMENT NOT APPROVED</b>\n\nOrder: <code>{{order_id}}</code>\n\nYour payment could not be verified.\nPlease contact support if you believe this was a mistake.",
    },
    "payment_rejected_admin": {
        "text": "<b>❌ PAYMENT REJECTED</b>\n\nOrder: <code>{{order_id}}</code> has been rejected.",
    },
    "key_recovery_error": {
        "text": "<b>⚠️ KEY RECOVERY ERROR</b>\n\nYour premium purchase exists, but the key could not be recovered.\n\nPlease contact support.",
    },
    "order_error": {
        "text": "<b>⚠️ ORDER ERROR</b>\n\nI couldn't open this premium order right now.\n\nPlease try again.",
    },
    "key_delivery": {
        "text": "<b>🔑 YOUR PREMIUM KEY</b>\n\n<code>{{key}}</code>\n\n⏰ Expires: <code>{{expires_at}}</code>",
        "product_button": "🌐 Open Product",
        "orders_button": "📦 My Orders",
    },
    "admin_review": {
        "buttons": [
            [
                {"text": "✅ Approve", "action": "approve"},
                {"text": "❌ Reject", "action": "reject"},
            ],
            [{"text": "🔎 Order Details", "action": "detail"}],
        ],
    },
    "alerts": {
        "payment_instructions_sent": "Payment instructions sent.",
        "plan_unavailable": "<b>⚠️ PLAN UNAVAILABLE</b>\n\nPlease choose another plan.",
        "pending_order_missing": "⚠️ I couldn't find your pending order right now. Please press Submit UTR again.",
        "invalid_utr": "❌ Please send a valid UTR/reference number.\n\nExample: <code>123456789012</code>",
        "order_load_failed": "⚠️ I couldn't load your order right now. Please try again.",
        "order_unavailable": "That order is no longer available.",
        "order_closed": "This order is already closed.",
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
        "no_pending_admin": "<b>📋 No pending payment verifications.</b>",
        "payment_instructions_sent": "Payment instructions sent."
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
        "admin_only": "Admin only.",
    },
}


def _deep_merge(default: Any, remote: Any) -> Any:
    if isinstance(default, dict) and isinstance(remote, dict):
        result = dict(default)
        for key, value in remote.items():
            result[key] = _deep_merge(result[key], value) if key in result else value
        return result
    return remote if remote is not None else default


def get_state(state: str) -> dict[str, Any]:
    """Read one UI state from Firebase, falling back safely to defaults."""
    try:
        remote = db.reference(f"{UI_ROOT}/{state}").get()
    except Exception:
        remote = None
    return _deep_merge(DEFAULT_UI.get(state, {}), remote or {})


def _safe_value(value: Any) -> str:
    return str(value if value is not None else "")


_TOKEN = re.compile(r"{{\s*([a-zA-Z0-9_.-]+)\s*}}")


def render(template: Any, values: Optional[dict[str, Any]] = None) -> Any:
    """Render {{variables}} without interpreting arbitrary code."""
    if not isinstance(template, str):
        return template

    values = values or {}

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value: Any = values
        for part in key.split("."):
            if isinstance(value, dict):
                value = value.get(part, "")
            else:
                return ""
        return _safe_value(value)

    return _TOKEN.sub(replace, template)


def state_text(
    state: str,
    values: Optional[dict[str, Any]] = None,
    field: str = "text",
) -> str:
    data = get_state(state)
    return str(render(data.get(field, ""), values))


def _button_from_config(button: dict[str, Any], values: dict[str, Any]) -> Optional[InlineKeyboardButton]:
    if button.get("enabled", True) is False:
        return None

    label = render(button.get("text", "Button"), values)
    style = button.get("style")

    url = render(button.get("url"), values) if button.get("url") else None

    if url:
        kwargs = {"text": label, "url": url}
    else:
        callback = button.get("callback")
        if callback is None:
            callback = button.get("action")
        callback = render(callback or "", values)
        if not callback:
            return None
        kwargs = {"text": label, "callback_data": callback}

    if style in {"primary", "success", "danger"}:
        kwargs["style"] = style

    return InlineKeyboardButton(**kwargs)


def keyboard(
    state: str,
    values: Optional[dict[str, Any]] = None,
    extra_rows: Optional[list[list[InlineKeyboardButton]]] = None,
    exclude_actions: Optional[set[str]] = None,
) -> InlineKeyboardMarkup:
    """Build a Telegram keyboard entirely from Firebase UI config."""
    values = values or {}
    data = get_state(state)
    rows = []

    for row in data.get("buttons", []):
        built = []
        for button in row:
            if exclude_actions and str(button.get("action", "")) in exclude_actions:
                continue
            item = _button_from_config(button, values)
            if item:
                built.append(item)
        if built:
            rows.append(built)

    if extra_rows:
        rows.extend(extra_rows)

    return InlineKeyboardMarkup(rows)


def dynamic_button(
    state: str,
    field: str,
    values: dict[str, Any],
    callback_data: str,
    style: Optional[str] = None,
) -> InlineKeyboardButton:
    data = get_state(state)
    label = render(data.get(field, "Button"), values)
    kwargs = {"text": label, "callback_data": callback_data}
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


def ui_config() -> dict[str, Any]:
    """Return the entire UI subtree for diagnostics/admin tooling."""
    try:
        return db.reference(UI_ROOT).get() or {}
    except Exception:
        return {}


def parse_mode(state: str = "") -> ParseMode:
    return ParseMode.HTML


# Compatibility alias for future extensions.
UI = type("UI", (), {
    "get_state": staticmethod(get_state),
    "render": staticmethod(render),
    "text": staticmethod(state_text),
    "keyboard": staticmethod(keyboard),
    "button": staticmethod(dynamic_button),
})
