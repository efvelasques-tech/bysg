#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
sys.path.insert(0, str(APP))


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env()

import database  # noqa: E402
import backups  # noqa: E402
import finance  # noqa: E402
import mailer  # noqa: E402
import payments  # noqa: E402
import webhooks  # noqa: E402


HOST = os.getenv("BYSG_HOST", "127.0.0.1")
PORT = int(os.getenv("BYSG_PORT", "4180"))
BASE_URL = os.getenv("BYSG_BASE_URL", f"http://{HOST}:{PORT}").rstrip("/")
SESSION_SECRET = os.getenv("BYSG_SESSION_SECRET", "bysg-local-development-secret-change-me")
ADMIN_PASSWORD = os.getenv("BYSG_ADMIN_PASSWORD", "bysg-admin-demo")
BAUTI_CODE = os.getenv("BYSG_BAUTI_CODE", "bauti")
MAX_BODY_BYTES = 256 * 1024
FINANCE_WORKBOOK = ROOT / "outputs" / "bysg-preventa-20261001" / "Finanzas-BYSG.xlsx"
PUBLIC_STATIC_FILES = frozenset({
    "index.html", "admin.html", "bauti.html", "checkout.html",
    "pago-aprobado.html", "pago-pendiente.html", "pago-rechazado.html",
    "seguimiento.html", "contacto.html", "arrepentimiento.html", "envios.html",
    "opiniones.html", "nosotros.html", "talles.html", "preguntas-frecuentes.html",
    "cambios-y-devoluciones.html", "privacidad.html", "terminos.html",
    "sistema.css", "sistema.js", "store-api.js", "estado-pedido.js", "public-config.js",
})
PUBLIC_ASSET_DIRS = frozenset({"branding", "productos", "talles"})
PUBLIC_IMAGE_SUFFIXES = frozenset({".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"})

RATE_LOCK = threading.Lock()
RATE_BUCKETS: dict[str, list[float]] = {}


def configured_mp_seller_id() -> str | None:
    seller_id = os.getenv("MP_SELLER_ID", "").strip()
    return seller_id if re.fullmatch(r"[1-9][0-9]{0,19}", seller_id) else None


def publication_readiness() -> dict:
    legal_identity_ready = all(
        os.getenv(key, "").strip()
        for key in ("BYSG_LEGAL_NAME", "BYSG_LEGAL_CUIT", "BYSG_CONTACT_EMAIL", "BYSG_BUSINESS_ADDRESS")
    )
    financials = database.category_financials()
    checks = [
        {"key": "https", "label": "Dominio confirmado con HTTPS", "ready": BASE_URL.startswith("https://")},
        {"key": "session_secret", "label": "Secreto de sesión cambiado", "ready": SESSION_SECRET != "bysg-local-development-secret-change-me" and len(SESSION_SECRET) >= 32},
        {"key": "admin_password", "label": "Clave de administración segura", "ready": ADMIN_PASSWORD != "bysg-admin-demo" and len(ADMIN_PASSWORD) >= 12},
        {"key": "bauti_code", "label": "Código de Bauti seguro", "ready": BAUTI_CODE != "bauti" and len(BAUTI_CODE) >= 8},
        {"key": "payments", "label": "Mercado Pago real", "ready": payments.payment_mode() == "mercado_pago" and bool(os.getenv("MP_ACCESS_TOKEN", "").strip())},
        {"key": "mp_seller", "label": "Vendedor esperado de Mercado Pago", "ready": configured_mp_seller_id() is not None},
        {"key": "webhook", "label": "Firma de webhooks", "ready": bool(os.getenv("MP_WEBHOOK_SECRET", "").strip())},
        {"key": "email", "label": "Correo automático", "ready": mailer.smtp_configured()},
        {"key": "legal_identity", "label": "Datos legales del titular adulto", "ready": legal_identity_ready},
        {"key": "final_prices", "label": "Precios finales", "ready": bool(financials) and all(not row["is_sample_price"] and row["price_cents"] > 0 for row in financials)},
    ]
    return {"ready": all(check["ready"] for check in checks), "checks": checks}


def production_mode_enabled() -> bool:
    """Treat an unrecognized non-empty production setting as enabled."""
    return os.getenv("BYSG_PRODUCTION", "").strip().lower() not in {"", "0", "false", "no", "off"}


def real_payments_enabled() -> bool:
    return (
        production_mode_enabled()
        and payments.payment_mode() == "mercado_pago"
        and publication_readiness()["ready"]
    )


def pending_checkout_payload(order: dict) -> dict:
    return {
        "status": "pending",
        "order_number": order["order_number"],
        "public_token": order["public_token"],
        "payment_status": order["payment_status"],
    }


def admin_dashboard() -> dict:
    result = database.dashboard()
    result["backups"] = backups.list_backups()
    result["publication"] = publication_readiness()
    result["email_configured"] = mailer.smtp_configured()
    return result


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def make_session(role: str, lifetime_seconds: int = 8 * 60 * 60) -> str:
    payload = {
        "role": role,
        "exp": int(time.time()) + lifetime_seconds,
        "nonce": secrets.token_hex(8),
    }
    encoded = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def read_session(token: str) -> dict | None:
    encoded, separator, signature = token.partition(".")
    if not separator:
        return None
    expected = hmac.new(SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def rate_allowed(ip: str, key: str, limit: int, window_seconds: int) -> bool:
    now = time.monotonic()
    bucket_key = f"{ip}:{key}"
    with RATE_LOCK:
        entries = [stamp for stamp in RATE_BUCKETS.get(bucket_key, []) if now - stamp < window_seconds]
        if len(entries) >= limit:
            RATE_BUCKETS[bucket_key] = entries
            return False
        entries.append(now)
        RATE_BUCKETS[bucket_key] = entries
        return True


class BYSGHandler(BaseHTTPRequestHandler):
    server_version = "BYSG/1.0"

    def log_message(self, message: str, *args) -> None:
        print(f"[{datetime.now().isoformat(timespec='seconds')}] {self.client_address[0]} {message % args}")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self' https://api.mercadopago.com; "
            "font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://www.mercadopago.com.ar",
        )
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        super().end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                return self._json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "BYSG",
                        "payment_mode": payments.payment_mode(),
                        "real_payments_enabled": real_payments_enabled(),
                        "database": "ready",
                    },
                )
            if path == "/api/catalog":
                return self._json(HTTPStatus.OK, database.catalog())
            if path == "/api/stock":
                return self._json(HTTPStatus.OK, {"stock": database.stock_status()})
            if path.startswith("/api/orders/"):
                token = path.rsplit("/", 1)[-1]
                return self._json(HTTPStatus.OK, database.get_order(token))
            if path == "/api/admin/dashboard":
                self._require_role("admin")
                return self._json(HTTPStatus.OK, admin_dashboard())
            if path == "/api/admin/finance-workbook":
                self._require_role("admin")
                return self._download_file(FINANCE_WORKBOOK, "Finanzas-BYSG.xlsx")
            if path == "/api/bauti/dashboard":
                self._require_any_role({"admin", "bauti"})
                dashboard = database.dashboard()
                dashboard["orders"] = [
                    {
                        "order_number": order["order_number"],
                        "customer_name": order["customer_name"].split(" ", 1)[0],
                        "units": order["units"],
                        "total_cents": order["total_cents"],
                        "payment_status": order["payment_status"],
                        "order_status": order["order_status"],
                        "created_at": order["created_at"],
                    }
                    for order in dashboard["orders"]
                ]
                return self._json(HTTPStatus.OK, dashboard)
            if path == "/api/public/config":
                return self._json(
                    HTTPStatus.OK,
                    {
                        "contact_email": os.getenv("BYSG_CONTACT_EMAIL", "").strip(),
                        "legal_name": os.getenv("BYSG_LEGAL_NAME", "").strip(),
                        "legal_cuit": os.getenv("BYSG_LEGAL_CUIT", "").strip(),
                        "business_address": os.getenv("BYSG_BUSINESS_ADDRESS", "").strip(),
                        "whatsapp": os.getenv("BYSG_WHATSAPP_URL", "").strip(),
                        "instagram": os.getenv("BYSG_INSTAGRAM_URL", "").strip(),
                        "tiktok": os.getenv("BYSG_TIKTOK_URL", "").strip(),
                        "youtube": os.getenv("BYSG_YOUTUBE_URL", "").strip(),
                    },
                )
            return self._serve_static(path)
        except KeyError as error:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
        except PermissionError as error:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(error)})
        except Exception as error:
            self._server_error(error)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/checkout/orders":
                payment_mode = payments.payment_mode()
                production_enabled = production_mode_enabled()
                if production_enabled:
                    checkout_allowed = real_payments_enabled()
                else:
                    checkout_allowed = payment_mode in {"mock", "mercado_pago_prueba"}
                may_resume_real_order = production_enabled and payment_mode == "mercado_pago"
                if not checkout_allowed and not may_resume_real_order:
                    return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "La tienda todavía no está habilitada para recibir pedidos."})
                self._require_rate("checkout", 12, 60)
                self._require_same_origin()
                payload = self._read_json()
                client_request_id = str(payload.pop("client_request_id", ""))
                if payment_mode == "mercado_pago":
                    existing = database.get_existing_order_for_request(payload, payment_mode, client_request_id)
                    if existing is not None:
                        return self._json(HTTPStatus.ACCEPTED, pending_checkout_payload(existing))
                    if not checkout_allowed:
                        return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "La tienda todavía no está habilitada para recibir pedidos."})
                    try:
                        seller_verified = payments.verify_seller_identity(configured_mp_seller_id() or "")
                    except payments.PaymentProviderError:
                        seller_verified = False
                    if not seller_verified:
                        return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "No se pudo verificar la cuenta de Mercado Pago. El pedido no se creó."})
                order, created = database.create_or_get_order(payload, payment_mode, client_request_id)
                pending_response = pending_checkout_payload(order)
                if not created:
                    return self._json(HTTPStatus.ACCEPTED, pending_response)
                try:
                    checkout = payments.create_checkout(order, BASE_URL)
                    database.attach_provider_order(order["public_token"], checkout["provider_order_id"])
                except Exception:
                    if payment_mode == "mock":
                        database.apply_payment_status(order["public_token"], "rejected", f"CREATE-FAILED-{secrets.token_hex(6)}")
                        raise
                    # A timeout, provider 5xx or malformed reply does not prove
                    # that Mercado Pago failed to create the remote order.
                    # Keep the reservation until the exact order is reconciled.
                    return self._json(HTTPStatus.ACCEPTED, pending_response)
                mailer.queue_order_event(order, "payment_pending", BASE_URL)
                mailer.deliver_pending()
                return self._json(
                    HTTPStatus.CREATED,
                    {
                        "order_number": order["order_number"],
                        "public_token": order["public_token"],
                        "checkout_url": checkout["checkout_url"],
                        "payment_mode": checkout["provider"],
                    },
                )
            if path == "/api/checkout/recover":
                session = getattr(self, "session", None) if payments.payment_mode() == "mercado_pago_prueba" else None
                if session is not None:
                    # The isolated runner keeps its verified test identity and
                    # Access Token only in this session, under one shared lock.
                    with session.lock:
                        if not session.connected:
                            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Sesión de prueba desconectada."})
                        return self._recover_checkout(session.seller_id, session.buyer_email)
                return self._recover_checkout(configured_mp_seller_id())
            if path == "/api/mock/payments":
                if production_mode_enabled() or payments.payment_mode() != "mock":
                    return self._json(HTTPStatus.NOT_FOUND, {"error": "La simulación de pagos no está habilitada."})
                self._require_rate("mock-payment", 20, 60)
                self._require_same_origin()
                payload = self._read_json()
                status = str(payload.get("status") or "")
                if status not in {"pending", "approved", "rejected"}:
                    raise ValueError("Estado simulado inválido.")
                token = str(payload.get("public_token") or "")
                payment_id = f"MOCKPAY-{secrets.token_hex(8).upper()}"
                order = database.apply_payment_status(token, status, payment_id, payload)
                if status == "approved":
                    mailer.queue_order_event(order, "payment_approved", BASE_URL)
                    mailer.deliver_pending()
                return self._json(HTTPStatus.OK, {"order": order})
            if path == "/api/webhooks/mercadopago":
                return self._handle_mercado_pago_webhook(parsed)
            if path == "/api/admin/login":
                self._require_rate("admin-login", 8, 15 * 60)
                payload = self._read_json()
                if not hmac.compare_digest(str(payload.get("password") or ""), ADMIN_PASSWORD):
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "Clave incorrecta."})
                return self._set_session("admin")
            if path == "/api/bauti/login":
                self._require_rate("bauti-login", 8, 15 * 60)
                payload = self._read_json()
                if not hmac.compare_digest(str(payload.get("code") or ""), BAUTI_CODE):
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "Código incorrecto."})
                return self._set_session("bauti")
            if path == "/api/logout":
                self.send_response(HTTPStatus.OK)
                self.send_header("Set-Cookie", "bysg_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
                return
            if path == "/api/admin/orders/status":
                self._require_role("admin")
                self._require_same_origin()
                payload = self._read_json()
                order = database.set_order_status(
                    str(payload.get("public_token") or ""),
                    str(payload.get("status") or ""),
                    payload.get("tracking") or {},
                )
                if str(payload.get("status") or "") == "shipped":
                    mailer.queue_order_event(order, "order_shipped", BASE_URL)
                    mailer.deliver_pending()
                return self._json(HTTPStatus.OK, {"order": order})
            if path == "/api/newsletter":
                self._require_rate("newsletter", 6, 60 * 60)
                self._require_same_origin()
                payload = self._read_json()
                return self._json(HTTPStatus.CREATED, database.subscribe_newsletter(str(payload.get("email") or "")))
            if path == "/api/contact":
                self._require_rate("contact", 6, 60 * 60)
                self._require_same_origin()
                payload = self._read_json()
                return self._json(
                    HTTPStatus.CREATED,
                    database.create_contact_message(
                        str(payload.get("name") or ""),
                        str(payload.get("email") or ""),
                        str(payload.get("message") or ""),
                    ),
                )
            if path == "/api/withdrawal":
                self._require_rate("withdrawal", 6, 60 * 60)
                self._require_same_origin()
                payload = self._read_json()
                return self._json(
                    HTTPStatus.CREATED,
                    database.create_withdrawal_request(
                        str(payload.get("order_number") or ""),
                        str(payload.get("email") or ""),
                        str(payload.get("detail") or ""),
                    ),
                )
            if path == "/api/admin/financials":
                self._require_role("admin")
                self._require_same_origin()
                payload = self._read_json()
                backups.create_backup(database.DATABASE_PATH, FINANCE_WORKBOOK, "antes-de-actualizar-precios")
                updated = database.update_category_financials(payload)
                finance.sync_workbook(FINANCE_WORKBOOK, database.category_financials())
                return self._json(HTTPStatus.OK, {"financial": updated})
            if path == "/api/admin/backups":
                self._require_role("admin")
                self._require_same_origin()
                backup = backups.create_backup(database.DATABASE_PATH, FINANCE_WORKBOOK, "manual")
                return self._json(HTTPStatus.CREATED, {"backup": backup, "backups": backups.list_backups()})
            if path == "/api/admin/emails/send":
                self._require_role("admin")
                self._require_same_origin()
                return self._json(HTTPStatus.OK, mailer.deliver_pending(50))
            if path == "/api/admin/bauti/movements":
                self._require_role("admin")
                self._require_same_origin()
                payload = self._read_json()
                movement = database.add_bauti_movement(
                    str(payload.get("concept") or "Movimiento manual"),
                    int(payload.get("debit_cents") or 0),
                    int(payload.get("credit_cents") or 0),
                )
                return self._json(HTTPStatus.CREATED, movement)
            self._json(HTTPStatus.NOT_FOUND, {"error": "Ruta no encontrada."})
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except KeyError as error:
            self._json(HTTPStatus.NOT_FOUND, {"error": str(error)})
        except PermissionError as error:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": str(error)})
        except payments.PaymentProviderError as error:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(error)})
        except Exception as error:
            self._server_error(error)

    def _recover_checkout(self, seller_id: str | None, sandbox_buyer_email: str | None = None) -> None:
        payment_mode = payments.payment_mode()
        if payment_mode not in {"mercado_pago", "mercado_pago_prueba"}:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Ruta no disponible."})
        if payment_mode == "mercado_pago" and not real_payments_enabled():
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "La recuperación del pago no está disponible."})
        if payment_mode == "mercado_pago_prueba":
            path = database.DATABASE_PATH.resolve()
            temporary_root = Path(tempfile.gettempdir()).resolve()
            if (
                sandbox_buyer_email is None
                or path.name != "pruebas.sqlite3"
                or not path.parent.name.startswith("bysg-pagos-prueba-")
                or path.parent.parent != temporary_root
            ):
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "La recuperación de prueba requiere una sesión aislada."})
        self._require_rate("checkout-recover", 6, 60)
        self._require_same_origin()
        token = str(self._read_json().get("public_token") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", token):
            raise ValueError("Identificador de pedido inválido.")
        order = database.get_order(token, include_private=True)
        if order["provider"] != payment_mode or (
            sandbox_buyer_email is not None and order["customer_email"].lower() != sandbox_buyer_email.lower()
        ):
            return self._json(HTTPStatus.CONFLICT, {"error": "El pedido no corresponde a esta sesión de pago."})
        result = pending_checkout_payload(order)
        if order["payment_status"] != "pending" or order["order_status"] != "payment_pending":
            return self._json(HTTPStatus.OK, result)
        if seller_id is None:
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "La recuperación del pago no está configurada."})
        try:
            remote = payments.recover_remote_order(order, seller_id)
        except payments.PaymentProviderError:
            return self._json(HTTPStatus.ACCEPTED, result)
        if remote is None:
            return self._json(HTTPStatus.ACCEPTED, result)
        remote_id = str(remote["id"])
        try:
            database.attach_provider_order(token, remote_id)
        except Exception:
            return self._json(HTTPStatus.ACCEPTED, result)
        provider_status = payments.payment_status_from_remote(remote)
        if sandbox_buyer_email is not None:
            # Payment-state mutation in the rehearsal remains an explicit
            # action on /local/pagos after a separate verified lookup.
            if provider_status != "pending":
                result["provider_payment_status"] = provider_status
                return self._json(HTTPStatus.OK, result)
        elif provider_status != "pending":
            summary = {
                "source": "provider_recovery",
                "provider_order_id": remote_id,
                "payment_status": provider_status,
                "currency": order["currency"],
                "total_amount_cents": order["total_cents"],
            }
            updated = database.apply_payment_status(token, provider_status, remote_id, summary)
            result["payment_status"] = updated["payment_status"]
            if updated["payment_status"] == "approved":
                mailer.queue_order_event(updated, "payment_approved", BASE_URL)
            return self._json(HTTPStatus.OK, result)
        checkout_url = payments.official_checkout_url(
            remote.get("checkout_url"), os.getenv("MP_ACCESS_TOKEN", ""), str(remote.get("client_token") or "")
        )
        if checkout_url is None:
            return self._json(HTTPStatus.ACCEPTED, result)
        result["status"] = "checkout_available"
        result["checkout_url"] = checkout_url
        return self._json(HTTPStatus.OK, result)

    def _handle_mercado_pago_webhook(self, parsed) -> None:
        seller_id = configured_mp_seller_id()
        if production_mode_enabled() and seller_id is None:
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Vendedor de Mercado Pago sin configurar. Reintentar."})
        query = parse_qs(parsed.query)
        identifiers = query.get("data.id") or []
        data_id = identifiers[0] if len(identifiers) == 1 else ""
        request_id = self.headers.get("x-request-id", "")
        signature = self.headers.get("x-signature", "")
        notifications = webhooks.OrderNotifications(
            expected_seller_id=seller_id,
            on_approved=lambda order: mailer.queue_order_event(order, "payment_approved", BASE_URL),
        )
        if not notifications.authenticate(signature, request_id, data_id):
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "Firma de webhook inválida."})
        try:
            result = notifications.process(data_id, self._read_json(), request_id)
        except webhooks.WebhookRejected:
            return self._json(HTTPStatus.BAD_REQUEST, {"error": "El aviso no corresponde al pedido. No se actualizó el pago."})
        except Exception:
            return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "No se pudo verificar el pago. Reintentar."})
        # SMTP is deliberately outside the provider's acknowledgement deadline.
        self._json(HTTPStatus.OK, result)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Content-Length inválido.") from error
        if length < 1 or length > MAX_BODY_BYTES:
            raise ValueError("El cuerpo de la solicitud está vacío o es demasiado grande.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("El cuerpo JSON no es válido.") from error
        if not isinstance(payload, dict):
            raise ValueError("Se esperaba un objeto JSON.")
        return payload

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, request_path: str) -> None:
        relative = unquote(request_path).lstrip("/")
        if relative.startswith("muestra-web/"):
            relative = relative[len("muestra-web/") :]
        if relative in {"", "muestra-web"}:
            relative = "index.html"
        requested_parts = Path(relative).parts
        if not requested_parts or any(part in {".", ".."} for part in requested_parts):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Archivo no encontrado."})
        root = ROOT.resolve()
        candidate = (root / relative).resolve()
        try:
            public_parts = candidate.relative_to(root).parts
        except ValueError:
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Archivo no encontrado."})
        public_file = len(public_parts) == 1 and (
            public_parts[0] in PUBLIC_STATIC_FILES
            or (public_parts[0] == "pago-prueba.html" and not production_mode_enabled())
        )
        public_asset = (
            len(public_parts) >= 3
            and public_parts[0] == "assets"
            and public_parts[1] in PUBLIC_ASSET_DIRS
            and candidate.suffix.lower() in PUBLIC_IMAGE_SUFFIXES
        )
        if any(part.startswith(".") for part in public_parts) or not (public_file or public_asset) or not candidate.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Archivo no encontrado."})
        content = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _download_file(self, path: Path, download_name: str) -> None:
        if not path.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "La planilla financiera todavía no está disponible."})
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _cookies(self) -> SimpleCookie:
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        return cookies

    def _role(self) -> str | None:
        morsel = self._cookies().get("bysg_session")
        payload = read_session(morsel.value) if morsel else None
        return payload.get("role") if payload else None

    def _require_role(self, role: str) -> None:
        if self._role() != role:
            raise PermissionError("Necesitás iniciar sesión.")

    def _require_any_role(self, roles: set[str]) -> None:
        if self._role() not in roles:
            raise PermissionError("Necesitás iniciar sesión.")

    def _set_session(self, role: str) -> None:
        token = make_session(role)
        secure = "; Secure" if BASE_URL.startswith("https://") else ""
        self.send_response(HTTPStatus.OK)
        self.send_header("Set-Cookie", f"bysg_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800{secure}")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        body = json.dumps({"status": "ok", "role": role}).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_rate(self, key: str, limit: int, window_seconds: int) -> None:
        if not rate_allowed(self.client_address[0], key, limit, window_seconds):
            raise PermissionError("Demasiados intentos. Esperá unos minutos.")

    def _require_same_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != BASE_URL:
            raise PermissionError("Origen no permitido.")

    def _server_error(self, error: Exception) -> None:
        print(f"ERROR {type(error).__name__}: {error}", file=sys.stderr)
        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Ocurrió un error interno. Revisá el registro del servidor."})


def main() -> None:
    database.initialize_database()
    backups.ensure_daily_backup(database.DATABASE_PATH, FINANCE_WORKBOOK)
    server = ThreadingHTTPServer((HOST, PORT), BYSGHandler)
    print(f"BYSG listo en {BASE_URL}")
    print(f"Tienda: {BASE_URL}/muestra-web/index.html")
    print(f"Administración: {BASE_URL}/admin.html")
    print(f"Consulta Bauti: {BASE_URL}/bauti.html")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
