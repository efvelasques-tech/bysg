from __future__ import annotations

import html
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import database


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "emails"

EVENTS = {
    "payment_pending": ("Pago pendiente · {order_number}", "pago-pendiente.html"),
    "payment_approved": ("Pedido confirmado · {order_number}", "pedido-confirmado.html"),
    "order_shipped": ("Tu pedido BYSG está en camino · {order_number}", "pedido-enviado.html"),
}


def _money(cents: int) -> str:
    return f"$ {round(cents / 100):,}".replace(",", ".")


def _tracking_link(order: dict[str, Any], base_url: str) -> str:
    return f"{base_url.rstrip('/')}/seguimiento.html?token={order['public_token']}"


def _render(order: dict[str, Any], template_name: str, base_url: str) -> str:
    template = (TEMPLATES / template_name).read_text(encoding="utf-8")
    items = "<br>".join(
        f"{html.escape(item['title'])} × {int(item['quantity'])}" for item in order.get("items", [])
    )
    replacements = {
        "ORDER_NUMBER": html.escape(order["order_number"]),
        "ORDER_ITEMS": items,
        "ORDER_TOTAL": _money(int(order["total_cents"])),
        "TRACKING_LINK": html.escape(_tracking_link(order, base_url), quote=True),
        "CARRIER": html.escape(order.get("tracking_provider") or "el correo"),
        "TRACKING_CODE": html.escape(order.get("tracking_code") or "A confirmar"),
        "TRACKING_URL": html.escape(order.get("tracking_url") or _tracking_link(order, base_url), quote=True),
    }
    for key, value in replacements.items():
        template = template.replace(f"{{{{{key}}}}}", value)
    return template


def queue_order_event(order: dict[str, Any], event_name: str, base_url: str) -> bool:
    if event_name not in EVENTS:
        return False
    subject_template, template_name = EVENTS[event_name]
    subject = subject_template.format(order_number=order["order_number"])
    body = _render(order, template_name, base_url)
    return database.queue_email(order, event_name, subject, template_name, body)


def smtp_configured() -> bool:
    return bool(os.getenv("SMTP_HOST", "").strip() and os.getenv("SMTP_FROM", "").strip())


def deliver_pending(limit: int = 20) -> dict[str, int | bool]:
    pending = database.pending_emails(limit)
    if not smtp_configured():
        return {"configured": False, "queued": len(pending), "sent": 0, "failed": 0}

    host = os.environ["SMTP_HOST"].strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    sender = os.environ["SMTP_FROM"].strip()
    use_ssl = os.getenv("SMTP_SSL", "false").lower() in {"1", "true", "yes"}
    use_starttls = os.getenv("SMTP_STARTTLS", "true").lower() in {"1", "true", "yes"}
    sent = failed = 0

    try:
        client: smtplib.SMTP
        if use_ssl:
            client = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(host, port, timeout=15)
            if use_starttls:
                client.starttls(context=ssl.create_default_context())
        if username:
            client.login(username, password)
        with client:
            for item in pending:
                try:
                    message = EmailMessage()
                    message["From"] = sender
                    message["To"] = item["recipient"]
                    message["Subject"] = item["subject"]
                    message.set_content("Este mensaje contiene una actualización de tu pedido BYSG.")
                    message.add_alternative(item["html_body"], subtype="html")
                    client.send_message(message)
                    database.mark_email_sent(item["id"])
                    sent += 1
                except Exception as error:  # The failed item remains available for a later retry.
                    database.mark_email_failed(item["id"], str(error))
                    failed += 1
    except Exception as error:
        for item in pending:
            database.mark_email_failed(item["id"], str(error))
            failed += 1

    return {"configured": True, "queued": max(0, len(pending) - sent - failed), "sent": sent, "failed": failed}
