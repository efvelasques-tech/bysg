"""Authenticated order reconciliation and a callback-only test listener.

The optional listener has no storefront, credentials page, admin API or files.
It remains bound to loopback; creating a public HTTPS connection is a separate,
explicitly authorized deployment step.
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import database
import payments


CALLBACK_PATH = "/api/webhooks/mercadopago"
MAX_BODY_BYTES = 64 * 1024
PROCESS_LOCK = threading.RLock()


class WebhookRejected(ValueError):
    """The notification cannot be reconciled to this store's order."""


def amount_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
            raise ValueError
        return int(amount)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise WebhookRejected("Importe remoto inválido.") from error


class OrderNotifications:
    def __init__(
        self,
        secret: Callable[[], str] | None = None,
        test_seller_id: str | None = None,
        on_approved: Callable[[dict], None] | None = None,
        on_processed: Callable[[dict], None] | None = None,
        on_failed: Callable[[str], None] | None = None,
        expected_seller_id: str | None = None,
    ):
        self.secret = secret or (lambda: os.getenv("MP_WEBHOOK_SECRET", "").strip())
        self.test_seller_id = test_seller_id
        self.expected_seller_id = expected_seller_id or test_seller_id
        self.on_approved = on_approved
        self.on_processed = on_processed
        self.on_failed = on_failed

    def report_failure(self, error: Exception) -> None:
        """Report only a fixed category/status, never exception text or URLs."""
        if not self.on_failed:
            return
        category = "internal_error"
        cause = error
        for _ in range(5):
            if isinstance(cause, urllib.error.HTTPError):
                category = f"provider_http_{int(cause.code)}"
                break
            if isinstance(cause, (urllib.error.URLError, TimeoutError)):
                category = "provider_connection_error"
                break
            cause = cause.__cause__
            if cause is None:
                break
        self.on_failed(category)

    def authenticate(self, signature: str, request_id: str, data_id: str) -> bool:
        return bool(
            re.fullmatch(r"ORD[A-Za-z0-9]{1,80}", data_id)
            and payments.verify_webhook_signature(signature, request_id, data_id, self.secret())
        )

    def process(self, data_id: str, payload: dict, request_id: str = "") -> dict:
        if payload.get("type") != "order":
            return {"status": "ignored"}
        data = payload.get("data")
        if not isinstance(data, dict) or str(data.get("id") or "") != data_id:
            raise WebhookRejected("El aviso no corresponde a la order firmada.")
        # Mercado Pago's simulator can omit the envelope's event ID. The
        # x-request-id header is included in the authenticated HMAC manifest,
        # so use that delivery ID only when the envelope ID is absent. Never
        # infer payment status from the simulator's illustrative data fields.
        event_id = str(payload.get("id") or request_id)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event_id):
            raise WebhookRejected("Identificador del aviso inválido.")
        if self.test_seller_id and (
            not data_id.startswith("ORDTST")
            or str(payload.get("user_id") or "") != self.test_seller_id
        ):
            raise WebhookRejected("El aviso no pertenece al vendedor de prueba.")
        # Only non-sensitive routing identifiers are persisted, never the
        # unsigned body, credentials, payer data or provider client tokens.
        receipt = {"type": "order", "data": {"id": data_id}}
        scoped_event_id = f"order:{data_id}:{event_id}"
        with PROCESS_LOCK:
            if not database.record_webhook(scoped_event_id, "order", receipt):
                return {"status": "duplicate"}
            # The browser return URL and notification body are not proof of
            # payment. Consult the provider with the server-side credential.
            remote = payments.fetch_remote_order(data_id)
            if not isinstance(remote, dict) or str(remote.get("id") or "") != data_id:
                raise WebhookRejected("Mercado Pago devolvió otra order.")
            if self.expected_seller_id and str(remote.get("user_id") or "") != self.expected_seller_id:
                raise WebhookRejected("La order no pertenece al vendedor configurado.")
            token = str(remote.get("external_reference") or "")
            try:
                order = database.get_order(token, include_private=True)
            except KeyError as error:
                raise WebhookRejected("La order no corresponde a un pedido de esta tienda.") from error
            if order.get("provider_order_id") != data_id or order.get("provider") not in {"mercado_pago", "mercado_pago_prueba"}:
                raise WebhookRejected("El pedido no está vinculado a esta order.")
            if remote.get("currency") != order["currency"] or amount_cents(remote.get("total_amount")) != order["total_cents"]:
                raise WebhookRejected("La moneda o el importe no coinciden con el pedido.")
            status = payments.payment_status_from_remote(remote)
            if status == "approved" and amount_cents(remote.get("total_paid_amount")) != order["total_cents"]:
                raise WebhookRejected("Mercado Pago no confirmó el importe completo.")
            summary = {
                "id": data_id,
                "status": str(remote.get("status") or ""),
                "status_detail": str(remote.get("status_detail") or ""),
                "currency": order["currency"],
                "total_amount": str(remote.get("total_amount")),
                "payment_status": status,
            }
            updated = database.apply_payment_status(token, status, data_id, summary)
            if updated["payment_status"] == "approved" and self.on_approved:
                # The mail outbox has a unique order/event key, so recovery
                # after a crash cannot enqueue a second confirmation.
                self.on_approved(updated)
            database.mark_webhook_processed(scoped_event_id)
            if self.on_processed:
                self.on_processed(updated)
            return {"status": "processed"}


class CallbackOnlyHandler(BaseHTTPRequestHandler):
    notifications: OrderNotifications
    server_version = "BYSG-Notifications/1.0"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, message: str, *args) -> None:
        pass

    def _json(self, code: HTTPStatus, body: dict) -> None:
        content = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        self._json(HTTPStatus.NOT_FOUND, {"error": "Ruta no disponible."})

    do_HEAD = do_GET
    do_OPTIONS = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_PATCH = do_GET

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Ruta no disponible."})
        query = parse_qs(parsed.query)
        identifiers = query.get("data.id") or []
        data_id = identifiers[0] if len(identifiers) == 1 else ""
        if not self.notifications.authenticate(
            self.headers.get("x-signature", ""), self.headers.get("x-request-id", ""), data_id
        ):
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "Firma inválida."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_BODY_BYTES:
                raise ValueError
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise ValueError
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            result = self.notifications.process(data_id, payload, self.headers.get("x-request-id", ""))
        except (ValueError, UnicodeDecodeError):
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "Aviso inválido; no se actualizó el pedido."})
        except Exception as error:
            # Do not acknowledge failure: Mercado Pago must be able to retry.
            self.notifications.report_failure(error)
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "No se pudo verificar el pago. Reintentar."})
        self._json(HTTPStatus.OK, result)
