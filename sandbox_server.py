#!/usr/bin/env python3
"""Loopback-only Mercado Pago test session; no credentials are written to disk."""
from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import ssl
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlsplit

import server as store
from secure_http import verified_ssl_context


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_test_token(token: str, seller_id: str) -> None:
    """Read-only identity check before enabling any order creation."""
    if not re.fullmatch(r"(?:APP_USR-|TEST-)[A-Za-z0-9_-]{20,500}", token):
        raise ValueError("Pegá el Access Token completo desde Credenciales de prueba.")
    request = urllib.request.Request(
        "https://api.mercadolibre.com/users/me",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    try:
        https = urllib.request.HTTPSHandler(context=verified_ssl_context())
        with urllib.request.build_opener(https, NoRedirect()).open(request, timeout=15) as response:
            profile = json.loads(response.read(64 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise ValueError("Mercado Pago no autorizó la verificación. Copiá nuevamente el Access Token de Credenciales de prueba; si persiste, informá este mensaje.") from error
        raise ValueError(f"Mercado Pago no pudo verificar la cuenta (HTTP {error.code}). Intentá nuevamente más tarde.") from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise ValueError("No se pudo validar la conexión HTTPS segura. El problema es del verificador local, no una confirmación de que tu clave sea incorrecta.") from error
        raise ValueError("No se pudo contactar a Mercado Pago. Revisá la conexión a Internet e intentá nuevamente.") from error
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        raise ValueError("No se pudo verificar la clave. Revisá que sea de prueba y que haya conexión a Internet.") from error
    if not isinstance(profile, dict):
        raise ValueError("Mercado Pago no devolvió una identidad verificable.")
    if str(profile.get("id")) != seller_id or "test_user" not in (profile.get("tags") or []):
        raise ValueError("Esta clave no pertenece al vendedor de prueba esperado. No se habilitó ningún pago.")
    if profile.get("site_id") != "MLA":
        raise ValueError("La cuenta de prueba debe ser de Argentina.")


class SandboxSession:
    def __init__(self, seller_id: str, buyer_id: str):
        if not seller_id.isdecimal() or not buyer_id.isdecimal() or seller_id == buyer_id:
            raise ValueError("Indicá IDs distintos y numéricos para vendedor y comprador de prueba.")
        self.seller_id = seller_id
        self.buyer_id = buyer_id
        self.buyer_email = f"testuser{buyer_id}@testuser.com"
        self.csrf = secrets.token_urlsafe(32)
        self.cookie = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.connected = False
        self.webhook_secret = ""
        self.webhook_port: int | None = None
        self.webhook_public_url = ""
        self.notifications_processed = 0
        self.last_notification: dict = {}
        self.disconnect()

    def connect(self, token: str, buyer_email: str) -> None:
        with self.lock:
            # Fail closed even when replacing an existing configuration.
            self.disconnect()
            if not re.fullmatch(r"[A-Za-z0-9._+-]+@testuser\.com", buyer_email):
                raise ValueError("Usá únicamente el correo del comprador de prueba, terminado en @testuser.com.")
            verify_test_token(token, self.seller_id)
            os.environ["MP_ACCESS_TOKEN"] = token
            os.environ["BYSG_PAYMENT_MODE"] = "mercado_pago_prueba"
            self.buyer_email = buyer_email
            self.connected = True

    def disconnect(self) -> None:
        with self.lock:
            os.environ.pop("MP_ACCESS_TOKEN", None)
            os.environ.pop("MP_WEBHOOK_SECRET", None)
            self.webhook_secret = ""
            # The isolated runner must not silently fall back to an existing
            # production token or the local payment simulator.
            os.environ["BYSG_PAYMENT_MODE"] = "sandbox_sin_clave"
            self.connected = False

    def configure_notifications(self, secret: str) -> None:
        with self.lock:
            if not self.connected or not self.webhook_port:
                raise ValueError("Primero conectá el vendedor de prueba e iniciá el receptor separado de avisos.")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
                raise ValueError("Copiá la clave secreta completa de Webhooks, no el Access Token ni una contraseña.")
            self.webhook_secret = secret

    def notification_processed(self, order: dict) -> None:
        with self.lock:
            self.notifications_processed += 1
            self.last_notification = {
                "order_number": order["order_number"],
                "payment_status": order["payment_status"],
                "received_at": store.database.utc_now(),
            }

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "sandbox": True,
            "seller_id": self.seller_id,
            "buyer_id": self.buyer_id,
            "buyer_email": self.buyer_email,
            "storage": "session_memory",
            "webhooks_ready": bool(self.notifications_processed),
            "webhook_public_url": self.webhook_public_url,
            "webhook_receiver_ready": bool(self.connected and self.webhook_port and self.webhook_secret),
            "webhook_port": self.webhook_port,
            "notifications_processed": self.notifications_processed,
            "last_notification": self.last_notification,
        }


def _sole_pending_test_order(buyer_email: str) -> dict:
    """Read one local rehearsal order without opening the database for writes."""
    path = store.database.DATABASE_PATH.resolve()
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT public_token, order_number, provider, provider_order_id,
                      customer_email, currency, total_cents, created_at
               FROM orders WHERE payment_status = 'pending' LIMIT 2"""
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("La copia debe tener exactamente un pedido de prueba pendiente para consultarlo.")
    order = dict(rows[0])
    if (
        order["provider"] != "mercado_pago_prueba"
        or order["customer_email"].lower() != buyer_email
        or not re.fullmatch(r"ORDTST[A-Za-z0-9]{1,80}", str(order["provider_order_id"] or ""))
        or order["currency"] != "ARS"
        or not isinstance(order["total_cents"], int)
        or order["total_cents"] <= 0
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(order["public_token"] or ""))
    ):
        raise ValueError("El pedido local no cumple las condiciones de este ensayo aislado.")
    return order


def _provider_http_code(error: Exception) -> int | None:
    cause = error.__cause__
    while cause is not None:
        if isinstance(cause, urllib.error.HTTPError):
            return int(cause.code)
        cause = cause.__cause__
    return None


def _remote_status_if_valid(remote: object, order: dict, seller_id: str) -> str | None:
    if not isinstance(remote, dict) or (
        not re.fullmatch(r"ORDTST[A-Za-z0-9]{1,80}", str(remote.get("id") or ""))
        or str(remote.get("user_id") or "") != seller_id
        or str(remote.get("external_reference") or "") != order["public_token"]
        or remote.get("currency") != order["currency"]
    ):
        return None
    try:
        if store.webhooks.amount_cents(remote.get("total_amount")) != order["total_cents"]:
            return None
        payment_status = store.payments.payment_status_from_remote(remote)
        if payment_status == "approved" and store.webhooks.amount_cents(remote.get("total_paid_amount")) != order["total_cents"]:
            return None
    except store.webhooks.WebhookRejected:
        return None
    return payment_status


def _search_pending_test_order(order: dict, session: SandboxSession) -> dict:
    """Bounded Orders search after an ID lookup fails; never treat a miss as proof of nonpayment."""
    try:
        created = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError
        created = created.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("La fecha del pedido local no permite una búsqueda segura.") from error
    fmt = lambda value: value.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urlencode({
        "begin_date": fmt(created - timedelta(days=1)),
        "end_date": fmt(created + timedelta(days=1)),
        "external_reference": order["public_token"],
        "limit": 10,
        "offset": 0,
    })
    try:
        response = store.payments._request_json(
            f"/v1/orders?{query}", method="GET", token=os.environ["MP_ACCESS_TOKEN"]
        )
    except store.payments.PaymentProviderError as error:
        code = _provider_http_code(error)
        if code in {401, 403}:
            return {
                "result": "search_unavailable", "id_match": False,
                "message": "La API de búsqueda no autorizó esta clave de prueba. No se puede concluir que el pedido o el pago no existan.",
            }
        return {
            "result": "search_error", "id_match": False,
            "message": "La búsqueda de prueba no pudo completarse. No se cambió el pedido local.",
        }
    if not isinstance(response, dict) or not isinstance(response.get("data"), list) or not isinstance(response.get("paging"), dict):
        return {"result": "search_incomplete", "id_match": False, "message": "Mercado Pago devolvió una búsqueda incompleta; el pedido no se modificó."}
    entries = response["data"]
    total_raw = response["paging"].get("total")
    if not str(total_raw).isdecimal() or len(entries) > 10:
        return {"result": "search_incomplete", "id_match": False, "message": "Mercado Pago no permitió verificar todos los resultados de la búsqueda."}
    total = int(total_raw)
    count = len(entries)
    if total == 0 and count == 0:
        return {
            "result": "search_empty", "candidate_count": 0, "id_match": False,
            "message": "La búsqueda por referencia y fecha no encontró esta order con la clave conectada. Esto no demuestra que el pago no exista.",
        }
    if total != 1 or count != 1:
        return {
            "result": "search_ambiguous", "candidate_count": count, "id_match": False,
            "message": "La búsqueda no devolvió un único resultado verificable. No se cambió el pedido.",
        }
    remote = entries[0]
    payment_status = _remote_status_if_valid(remote, order, session.seller_id)
    if payment_status is None:
        return {
            "result": "search_mismatch", "candidate_count": 1, "id_match": False,
            "message": "El resultado no coincide en identidad, vendedor, moneda o importe con el pedido local.",
        }
    id_match = remote["id"] == order["provider_order_id"]
    return {
        "result": "verified" if id_match else "different_order_id",
        "source": "search", "candidate_count": 1, "id_match": id_match,
        "order_number": order["order_number"],
        "provider_payment_status": payment_status,
        "local_payment_status": "pending",
        "message": (
            "La búsqueda encontró la misma order. Esta consulta no modificó el pedido local."
            if id_match else
            "La referencia coincide, pero Mercado Pago devolvió otro ID de order; no se puede confirmar el pedido local."
        ),
    }


def check_pending_test_order(session: SandboxSession) -> dict:
    """Consult Mercado Pago, return only a fixed diagnostic; never update an order."""
    with session.lock:
        if not session.connected or store.payments.payment_mode() != "mercado_pago_prueba":
            raise ValueError("Primero conectá el Access Token del vendedor de prueba.")
        order = _sole_pending_test_order(session.buyer_email)
        try:
            remote = store.payments.fetch_remote_order(order["provider_order_id"])
        except store.payments.PaymentProviderError as error:
            code = _provider_http_code(error)
            if code == 404:
                return _search_pending_test_order(order, session)
            if code in {401, 403}:
                return {"result": "not_authorized", "message": "Mercado Pago no autorizó consultar la order con esta clave de prueba."}
            return {"result": "provider_error", "message": "No se pudo consultar la order en Mercado Pago. El pedido local sigue sin cambios."}
        mismatch = {"result": "mismatch", "message": "La respuesta de Mercado Pago no coincide con el pedido y vendedor de prueba. No se cambió el pedido local."}
        if not isinstance(remote, dict) or str(remote.get("id") or "") != order["provider_order_id"]:
            return mismatch
        payment_status = _remote_status_if_valid(remote, order, session.seller_id)
        if payment_status is None:
            return mismatch
        return {
            "result": "verified",
            "order_number": order["order_number"],
            "provider_payment_status": payment_status,
            "local_payment_status": "pending",
            "message": "Mercado Pago confirmó la identidad de la order. Esta consulta no modifica el pedido local.",
        }


def reconcile_pending_test_order(session: SandboxSession) -> dict:
    """Approve one isolated rehearsal order only after a fresh provider lookup.

    This is an explicit alternative to a missing webhook, not a callback
    simulator. Never infer payment from the browser return or a saved receipt.
    """
    with session.lock:
        if not session.connected or store.payments.payment_mode() != "mercado_pago_prueba":
            raise ValueError("Primero conectá el Access Token del vendedor de prueba.")
        path = store.database.DATABASE_PATH.resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if (
            path.name != "pruebas.sqlite3"
            or not path.parent.name.startswith("bysg-pagos-prueba-")
            or path.parent.parent != temporary_root
        ):
            raise ValueError("La conciliación solo está disponible en una copia temporal del ensayo.")
        order = _sole_pending_test_order(session.buyer_email)
        try:
            remote = store.payments.fetch_remote_order(order["provider_order_id"])
        except store.payments.PaymentProviderError as error:
            code = _provider_http_code(error)
            result = "not_authorized" if code in {401, 403} else "provider_unavailable"
            return {"result": result, "message": "Mercado Pago no permitió verificar esta order de prueba por ID. El pedido no se modificó."}
        if not isinstance(remote, dict) or str(remote.get("id") or "") != order["provider_order_id"]:
            return {"result": "mismatch", "message": "La order remota no coincide con el pedido de prueba. No se cambió su estado."}
        payment_status = _remote_status_if_valid(remote, order, session.seller_id)
        if payment_status is None:
            return {"result": "mismatch", "message": "La identidad, moneda o importe remoto no coinciden con el pedido de prueba. No se cambió su estado."}
        if payment_status != "approved":
            return {
                "result": "not_approved", "order_number": order["order_number"],
                "provider_payment_status": payment_status,
                "message": "Mercado Pago todavía no confirma el pago completo. El pedido no se modificó.",
            }
        # Other local mutations use DB_LOCK. Hold it across the final local
        # recheck and update so a cancellation cannot slip between them.
        with store.database.DB_LOCK:
            if _sole_pending_test_order(session.buyer_email) != order:
                raise ValueError("El pedido cambió mientras se comprobaba. Volvé a consultarlo.")
            summary = {
                "source": "manual_sandbox_reconciliation",
                "provider_order_id": order["provider_order_id"],
                "payment_status": "approved",
                "currency": order["currency"],
                "total_amount_cents": order["total_cents"],
            }
            updated = store.database.apply_payment_status(
                order["public_token"], "approved", order["provider_order_id"], summary
            )
        if updated["payment_status"] != "approved":
            raise ValueError("No se pudo confirmar el pedido local. Revisá su estado antes de intentar de nuevo.")
        return {
            "result": "reconciled", "order_number": order["order_number"],
            "provider_payment_status": "approved", "local_payment_status": "approved",
            "message": "Pago de prueba conciliado con Mercado Pago. El pedido local ahora figura pagado; los avisos automáticos siguen pendientes de configuración.",
        }


def _safe_provider_state(value: object) -> str | None:
    """Return only short status codes, never arbitrary provider response text."""
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) else None


def _official_checkout_url(value: object, secrets_to_hide: tuple[str, ...]) -> str | None:
    if not isinstance(value, str) or re.search(r"[\s\\]", value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.mercadopago.com.ar", "www.mercadopago.com"}
        or parsed.netloc.lower() != parsed.hostname
        or not (parsed.path == "/checkout" or parsed.path.startswith("/checkout/"))
        or parsed.fragment
        or any(key.lower() in {"access_token", "client_token"} for key, _ in parse_qsl(parsed.query))
        or any(secret and secret in unquote(value) for secret in secrets_to_hide)
    ):
        return None
    return value


def recover_pending_test_checkout(session: SandboxSession) -> dict:
    """Read the exact pending test order and expose only its official checkout URL."""
    with session.lock:
        if not session.connected or store.payments.payment_mode() != "mercado_pago_prueba":
            raise ValueError("Primero conectá el Access Token del vendedor de prueba.")
        order = _sole_pending_test_order(session.buyer_email)
        try:
            remote = store.payments.fetch_remote_order(order["provider_order_id"])
        except store.payments.PaymentProviderError as error:
            code = _provider_http_code(error)
            if code in {401, 403}:
                return {"result": "not_authorized", "message": "Mercado Pago no autorizó consultar esta order de prueba."}
            return {"result": "provider_unavailable", "message": "Mercado Pago no permitió consultar esta order de prueba por ID."}
        mismatch = {"result": "mismatch", "message": "La order remota no coincide con el pedido y vendedor de prueba."}
        if not isinstance(remote, dict) or str(remote.get("id") or "") != order["provider_order_id"]:
            return mismatch
        payment_status = _remote_status_if_valid(remote, order, session.seller_id)
        if payment_status is None:
            return mismatch
        expected_app_id = os.environ.get("MP_APP_ID", "").strip()
        remote_app_id = remote.get("app_id")
        app_id_match = (
            str(remote_app_id) == expected_app_id
            if expected_app_id.isdecimal() and str(remote_app_id or "").isdecimal()
            else None
        )
        if app_id_match is False:
            return mismatch
        checkout_url = remote.get("checkout_url")
        secrets_to_hide = (os.environ.get("MP_ACCESS_TOKEN", ""), str(remote.get("client_token") or ""))
        status = _safe_provider_state(remote.get("status"))
        status_detail = _safe_provider_state(remote.get("status_detail"))
        diagnostic = {
            "provider_order_status": None if status and any(secret and secret in status for secret in secrets_to_hide) else status,
            "provider_status_detail": None if status_detail and any(secret and secret in status_detail for secret in secrets_to_hide) else status_detail,
            "checkout_url_present": isinstance(checkout_url, str) and bool(checkout_url),
            "app_id_match": app_id_match,
        }
        if not diagnostic["checkout_url_present"]:
            return {"result": "missing_checkout_url", **diagnostic,
                    "message": "La consulta por ID no devolvió checkout_url; no hay un enlace oficial para recuperar."}
        official_url = _official_checkout_url(checkout_url, secrets_to_hide)
        if official_url is None:
            return {"result": "invalid_checkout_url", **diagnostic,
                    "message": "Mercado Pago devolvió un enlace fuera de los dominios HTTPS permitidos."}
        if payment_status != "pending":
            return {"result": "not_pending", **diagnostic,
                    "message": "Mercado Pago ya no informa esta order como pendiente."}
        return {"result": "checkout_available", **diagnostic, "checkout_url": official_url}


class SandboxHandler(store.BYSGHandler):
    session: SandboxSession
    origin: str

    def log_message(self, message: str, *args) -> None:
        # No request URLs, bodies, headers, or credentials in terminal logs.
        pass

    def _allowed_local(self) -> bool:
        expected_host = urlparse(self.origin).netloc
        try:
            loopback = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            loopback = False
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        return (
            loopback
            and self.headers.get("Host") == expected_host
            and (origin is None or origin == self.origin)
            and site not in {"cross-site", "same-site"}
        )

    def _private_post(self) -> None:
        cookies = SimpleCookie()
        cookies.load(self.headers.get("Cookie", ""))
        cookie = cookies.get("bysg_sandbox")
        if (
            self.headers.get("Origin") != self.origin
            or not cookie
            or not hmac.compare_digest(cookie.value, self.session.cookie)
            or not hmac.compare_digest(self.headers.get("X-BYSG-CSRF", ""), self.session.csrf)
            or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"
        ):
            raise PermissionError("Abrí la pantalla local de configuración para continuar.")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if urlparse(self.path).path == "/local/pagos":
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; script-src 'nonce-{self.session.csrf}'; "
                "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'none'",
            )
        else:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; "
                "form-action 'self' https://www.mercadopago.com.ar",
            )
        BaseHTTPRequestHandler.end_headers(self)

    def _serve_static(self, request_path: str) -> None:
        relative = unquote(request_path).lstrip("/")
        if relative.startswith("muestra-web/"):
            relative = relative[len("muestra-web/"):]
        parts = Path(relative).parts
        public_types = {".html", ".css", ".js", ".json", ".svg", ".png", ".webp", ".jpg", ".jpeg", ".ico", ".woff", ".woff2"}
        if (
            any(part.startswith(".") or part in {"app", "data", "backups", "outputs", "tests"} for part in parts)
            or (relative and Path(relative).suffix.lower() not in public_types)
        ):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Archivo no encontrado."})
        super()._serve_static(request_path)

    def do_GET(self) -> None:  # noqa: N802
        if not self._allowed_local():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Esta sesión solo funciona desde esta Mac."})
        path = urlparse(self.path).path
        if path == "/local/pagos":
            template = (store.APP / "sandbox-config.html").read_text(encoding="utf-8")
            body = template.replace("{{CSRF}}", self.session.csrf).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", f"bysg_sandbox={self.session.cookie}; Path=/; HttpOnly; SameSite=Strict")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/local/pagos":
            return self._json(HTTPStatus.OK, self.session.status())
        if path == "/api/health":
            return self._json(HTTPStatus.OK, {
                "status": "ok", "service": "BYSG", "database": "test_only",
                "sandbox": True, "payment_mode": store.payments.payment_mode(),
                "test_buyer_email": self.session.buyer_email,
                "payments_configured": self.session.connected,
            })
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed_local():
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Origen local no permitido."})
        path = urlparse(self.path).path
        order_actions = {
            "/api/local/pagos/comprobar-pedido": check_pending_test_order,
            "/api/local/pagos/recuperar-checkout": recover_pending_test_checkout,
            "/api/local/pagos/conciliar-pedido": reconcile_pending_test_order,
        }
        if path in order_actions:
            try:
                self._private_post()
                mutating = path == "/api/local/pagos/conciliar-pedido"
                self._require_rate(
                    "sandbox-order-reconcile" if mutating else "sandbox-order-check",
                    3 if mutating else 4,
                    60,
                )
                if self._read_json():
                    raise ValueError("Esta comprobación no admite datos adicionales.")
                result = order_actions[path](self.session)
                return self._json(HTTPStatus.OK, result)
            except PermissionError:
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Volvé a abrir la pantalla local para continuar."})
            except ValueError as error:
                return self._json(HTTPStatus.CONFLICT, {"error": str(error)})
            except Exception:
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "No se pudo comprobar el pedido de prueba. No se cambió su estado."})
        if path in {"/api/local/pagos/conectar", "/api/local/pagos/desconectar", "/api/local/pagos/avisos"}:
            try:
                self._private_post()
                self._require_rate("sandbox-setup", 8, 60)
                payload = self._read_json()
                if path.endswith("/desconectar"):
                    self.session.disconnect()
                elif path.endswith("/avisos"):
                    self.session.configure_notifications(str(payload.get("webhook_secret") or "").strip())
                else:
                    self.session.connect(
                        str(payload.get("access_token") or "").strip(),
                        str(payload.get("buyer_email") or "").strip().lower(),
                    )
                return self._json(HTTPStatus.OK, self.session.status())
            except PermissionError:
                return self._json(HTTPStatus.FORBIDDEN, {"error": "Volvé a abrir la pantalla local para continuar."})
            except ValueError as error:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception:
                return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "No se pudo configurar la sesión. La clave no se guardó en disco."})
        # Keep every sandbox checkout under the same lock as connect/disconnect.
        # There is no real buyer email, SMTP delivery, or production DB access.
        if path == "/api/checkout/orders":
            with self.session.lock:
                if not self.session.connected:
                    return self._json(HTTPStatus.CONFLICT, {"error": "Primero conectá la clave de prueba en /local/pagos."})
                super().do_POST()
            return
        if path.startswith("/api/admin/") and path not in {"/api/admin/login", "/api/admin/orders/status"}:
            return self._json(HTTPStatus.FORBIDDEN, {"error": "Esta acción no está disponible en la sesión de pagos de prueba."})
        super().do_POST()

    def _read_json(self) -> dict:
        payload = super()._read_json()
        if urlparse(self.path).path == "/api/checkout/orders":
            customer = payload.get("customer")
            if not isinstance(customer, dict) or str(customer.get("email", "")).lower() != self.session.buyer_email:
                raise ValueError("El pedido debe usar el correo del comprador de prueba configurado.")
        return payload

    def _server_error(self, error: Exception) -> None:
        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "No se pudo completar la operación de prueba."})


class SandboxCallbackHandler(store.webhooks.CallbackOnlyHandler):
    session: SandboxSession

    def do_POST(self) -> None:  # noqa: N802
        # Prevent an in-flight callback from using a replaced/disconnected key.
        with self.session.lock:
            if not self.session.connected:
                return self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Sesión de prueba desconectada."})
            super().do_POST()


def copy_test_database(source: Path, destination: Path, buyer_email: str) -> None:
    """Snapshot an existing isolated rehearsal without modifying its session."""
    source = source.resolve(strict=True)
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        source.name != "pruebas.sqlite3"
        or not source.parent.name.startswith("bysg-pagos-prueba-")
        or source.parent.parent != temporary_root
    ):
        raise ValueError("Solo se admite una base temporal del ensayo aislado.")
    # A read-only SQLite backup includes committed WAL data atomically. No
    # credentials or process memory are copied and the old runner stays intact.
    with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as original:
        with sqlite3.connect(destination) as snapshot:
            original.backup(snapshot)
            rows = snapshot.execute(
                "SELECT provider, provider_order_id, customer_email FROM orders"
            ).fetchall()
            if any(
                provider != "mercado_pago_prueba"
                or not str(order_id or "").startswith("ORDTST")
                or email.lower() != buyer_email
                for provider, order_id, email in rows
            ):
                raise ValueError("La copia contiene pedidos ajenos a este ensayo.")
            if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("No se pudo validar la copia del ensayo.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seller-id", required=True)
    parser.add_argument("--buyer-id", required=True)
    parser.add_argument("--port", type=int, default=4181)
    parser.add_argument("--webhook-port", type=int, help="Receptor separado, solo loopback; no publica la tienda.")
    parser.add_argument("--webhook-url", default="", help="URL temporal ya autorizada del receptor, nunca de la tienda.")
    parser.add_argument("--resume-test-db", type=Path, help="Copiar un ensayo temporal existente sin detenerlo.")
    args = parser.parse_args()
    if args.webhook_port is not None and (not 1 <= args.webhook_port <= 65535 or args.webhook_port == args.port):
        parser.error("El puerto de avisos debe ser válido y distinto del puerto privado.")
    if args.webhook_url and (not args.webhook_port or not re.fullmatch(
        r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com/api/webhooks/mercadopago", args.webhook_url
    )):
        parser.error("La URL debe corresponder al receptor temporal de Cloudflare autorizado.")
    session = SandboxSession(args.seller_id, args.buyer_id)
    session.webhook_public_url = args.webhook_url
    origin = f"http://127.0.0.1:{args.port}"
    with tempfile.TemporaryDirectory(prefix="bysg-pagos-prueba-") as scratch:
        # A fresh temporary database, not a copy of customer/business records.
        store.database.DATABASE_PATH = Path(scratch) / "pruebas.sqlite3"
        store.backups.BACKUP_ROOT = Path(scratch) / "backups"
        store.FINANCE_WORKBOOK = Path(scratch) / "Finanzas-prueba.xlsx"
        store.BASE_URL = origin
        store.SESSION_SECRET = secrets.token_urlsafe(48)
        os.environ["SMTP_HOST"] = ""
        os.environ["SMTP_FROM"] = ""
        os.environ["BYSG_PRODUCTION"] = "false"
        os.environ.pop("MP_WEBHOOK_SECRET", None)
        if args.resume_test_db:
            copy_test_database(args.resume_test_db, store.database.DATABASE_PATH, session.buyer_email)
        store.database.initialize_database()
        SandboxHandler.session = session
        SandboxHandler.origin = origin
        receiver = None
        receiver_thread = None
        if args.webhook_port:
            SandboxCallbackHandler.session = session
            SandboxCallbackHandler.notifications = store.webhooks.OrderNotifications(
                secret=lambda: session.webhook_secret,
                test_seller_id=session.seller_id,
                on_processed=session.notification_processed,
                on_failed=lambda category: print(f"Comprobación de aviso de prueba: {category}.", flush=True),
            )
            receiver = ThreadingHTTPServer(("127.0.0.1", args.webhook_port), SandboxCallbackHandler)
            receiver.daemon_threads = True
            session.webhook_port = receiver.server_port
            receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
            receiver_thread.start()
            connection_note = "enlace temporal externo indicado; debe verificarse" if args.webhook_url else "conexión pública NO habilitada"
            print(f"Receptor de avisos separado: 127.0.0.1:{receiver.server_port}; {connection_note}.", flush=True)
        with ThreadingHTTPServer(("127.0.0.1", args.port), SandboxHandler) as httpd:
            print(f"Pantalla privada de prueba: {origin}/local/pagos", flush=True)
            print("Clave solo en memoria; pedidos aislados; correos desactivados.", flush=True)
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                session.disconnect()
                if receiver:
                    receiver.shutdown()
                    receiver.server_close()
                    receiver_thread.join()


if __name__ == "__main__":
    main()
