from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from secure_http import verified_ssl_context


MERCADO_PAGO_API = "https://api.mercadopago.com"
MERCADO_LIBRE_IDENTITY_API = "https://api.mercadolibre.com/users/me"


class PaymentProviderError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def payment_mode() -> str:
    return os.getenv("BYSG_PAYMENT_MODE", "mock").strip().lower()


def _money(cents: int) -> str:
    return f"{(Decimal(cents) / Decimal(100)):.2f}"


def verify_seller_identity(expected_seller_id: str) -> bool:
    """Check the configured credential's owner before reserving real stock."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", expected_seller_id):
        return False
    token = os.getenv("MP_ACCESS_TOKEN", "").strip()
    if not token:
        return False
    request = urllib.request.Request(
        MERCADO_LIBRE_IDENTITY_API,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "BYSG-Store/1.0"},
        method="GET",
    )
    try:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=verified_ssl_context()), _NoRedirect()
        )
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise PaymentProviderError("Mercado Pago no confirmó la identidad del vendedor.")
            profile = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise PaymentProviderError("No se pudo verificar el titular de la cuenta de Mercado Pago.") from error
    if not isinstance(profile, dict) or not isinstance(profile.get("tags"), list):
        return False
    return (
        str(profile.get("id") or "") == expected_seller_id
        and profile.get("site_id") == "MLA"
        and "test_user" not in profile["tags"]
    )


def create_checkout(order: dict[str, Any], base_url: str) -> dict[str, Any]:
    if payment_mode() == "mock":
        return {
            "provider": "mock",
            "provider_order_id": f"MOCK-{uuid.uuid4().hex[:16].upper()}",
            "checkout_url": f"{base_url}/pago-prueba.html?token={order['public_token']}",
        }
    token = os.getenv("MP_ACCESS_TOKEN", "").strip()
    if not token:
        raise PaymentProviderError("Falta MP_ACCESS_TOKEN para activar Mercado Pago.")
    payload = {
        "type": "online",
        "processing_mode": "manual",
        "capture_mode": "automatic_async",
        "total_amount": _money(order["total_cents"]),
        "external_reference": order["public_token"],
        "payer": {
            "email": order["customer_email"],
            "first_name": order["customer_name"].split(" ", 1)[0],
            "last_name": order["customer_name"].split(" ", 1)[1] if " " in order["customer_name"] else "BYSG",
        },
        "config": {
            "statement_descriptor": "BYSG",
            "online": {
                "success_url": f"{base_url}/pago-aprobado.html?token={order['public_token']}",
                "pending_url": f"{base_url}/pago-pendiente.html?token={order['public_token']}",
                "failure_url": f"{base_url}/pago-rechazado.html?token={order['public_token']}",
                "auto_return": "approved",
            },
            "payment_method": {"max_installments": 12},
        },
        "items": [
            {
                "external_code": item["sku"],
                "title": item["title"],
                "description": f"Preventa BYSG · {item['size_label']}",
                "category_id": "apparel",
                "quantity": item["quantity"],
                "unit_price": _money(item["unit_price_cents"]),
                "type": "apparel",
                "warranty": False,
            }
            for item in order["items"]
        ],
        "description": f"Pedido {order['order_number']} · Preventa BYSG",
    }
    # The isolated local runner has no public HTTPS return URL. The buyer can
    # inspect the result in Mercado Pago; automatic local updates need webhooks.
    if payment_mode() == "mercado_pago_prueba":
        payload["config"].pop("online", None)
    response = _request_json(
        "/v1/orders",
        method="POST",
        token=token,
        payload=payload,
        # A retry for this *same local order* must not create another remote
        # order if the first response was lost after Mercado Pago accepted it.
        extra_headers={"X-Idempotency-Key": str(uuid.uuid5(uuid.NAMESPACE_URL, f"bysg-order:{order['public_token']}"))},
    )
    if not isinstance(response, dict):
        raise PaymentProviderError("Mercado Pago devolvió una respuesta de order inválida.")
    provider_order_id = str(response.get("id") or "")
    if not re.fullmatch(r"ORD[A-Za-z0-9]{1,80}", provider_order_id):
        raise PaymentProviderError("Mercado Pago devolvió un ID de order inválido.")
    if payment_mode() == "mercado_pago_prueba" and not provider_order_id.startswith("ORDTST"):
        raise PaymentProviderError("La respuesta no corresponde a una order de prueba. No se abrirá el pago.")
    if payment_mode() == "mercado_pago" and provider_order_id.startswith("ORDTST"):
        raise PaymentProviderError("La respuesta corresponde a una order de prueba. No se abrirá el pago.")
    if payment_mode() == "mercado_pago":
        expected_seller_id = os.getenv("MP_SELLER_ID", "").strip()
        if not re.fullmatch(r"[1-9][0-9]{0,19}", expected_seller_id) or str(response.get("user_id") or "") != expected_seller_id:
            raise PaymentProviderError("La order no pertenece al vendedor configurado. No se abrirá el pago.")
    checkout_url = official_checkout_url(response.get("checkout_url"), token, str(response.get("client_token") or ""))
    if not checkout_url:
        raise PaymentProviderError("Mercado Pago no devolvió un enlace de pago verificable.")
    return {
        "provider": "mercado_pago",
        "provider_order_id": provider_order_id,
        "checkout_url": checkout_url,
    }


def fetch_remote_order(provider_order_id: str) -> dict[str, Any]:
    # Never interpolate a path, query string, or arbitrary resource into an
    # authenticated request. The test runner cannot consult production orders.
    if not re.fullmatch(r"ORD[A-Za-z0-9]{1,80}", provider_order_id):
        raise PaymentProviderError("Identificador de order inválido.")
    if payment_mode() == "mercado_pago_prueba" and not provider_order_id.startswith("ORDTST"):
        raise PaymentProviderError("Solo se pueden consultar orders de prueba.")
    token = os.getenv("MP_ACCESS_TOKEN", "").strip()
    if not token:
        raise PaymentProviderError("Falta MP_ACCESS_TOKEN.")
    return _request_json(f"/v1/orders/{provider_order_id}", method="GET", token=token)


def official_checkout_url(value: object, access_token: str = "", client_token: str = "") -> str | None:
    """Only hand a buyer an HTTPS Checkout Pro URL from Mercado Pago."""
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
        or any(secret and secret in unquote(value) for secret in (access_token, client_token))
    ):
        return None
    return value


def _amount_cents(value: object) -> int | None:
    try:
        cents = Decimal(str(value)) * 100
        if cents.is_finite() and cents >= 0 and cents == cents.to_integral_value():
            return int(cents)
    except (InvalidOperation, TypeError, ValueError):
        pass
    return None


def _remote_matches_order(remote: object, order: dict[str, Any], seller_id: str) -> bool:
    if not isinstance(remote, dict):
        return False
    remote_id = str(remote.get("id") or "")
    if (
        not re.fullmatch(r"ORD[A-Za-z0-9]{1,80}", remote_id)
        or (order["provider"] == "mercado_pago_prueba") != remote_id.startswith("ORDTST")
        or str(remote.get("user_id") or "") != seller_id
        or str(remote.get("external_reference") or "") != order["public_token"]
        or remote.get("currency") != order["currency"]
        or _amount_cents(remote.get("total_amount")) != order["total_cents"]
    ):
        return False
    if payment_status_from_remote(remote) == "approved" and _amount_cents(remote.get("total_paid_amount")) != order["total_cents"]:
        return False
    return True


def recover_remote_order(order: dict[str, Any], seller_id: str) -> dict[str, Any] | None:
    """Find one exact provider order; a search miss or ambiguity proves nothing."""
    if not re.fullmatch(r"[1-9][0-9]{0,19}", seller_id):
        return None
    token = os.getenv("MP_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    known_id = str(order.get("provider_order_id") or "")
    if known_id:
        remote = fetch_remote_order(known_id)
        return remote if isinstance(remote, dict) and str(remote.get("id") or "") == known_id and _remote_matches_order(remote, order, seller_id) else None

    try:
        created = datetime.fromisoformat(str(order["created_at"]).replace("Z", "+00:00"))
        if created.tzinfo is None:
            return None
        created = created.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    query = urlencode({
        "begin_date": (created - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date": (created + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "external_reference": order["public_token"],
        "limit": 10,
        "offset": 0,
    })
    search = _request_json(f"/v1/orders?{query}", method="GET", token=token)
    if not isinstance(search, dict) or not isinstance(search.get("data"), list) or not isinstance(search.get("paging"), dict):
        return None
    entries = search["data"]
    total = search["paging"].get("total")
    if str(total) != "1" or len(entries) != 1 or not isinstance(entries[0], dict):
        return None
    remote_id = str(entries[0].get("id") or "")
    if not re.fullmatch(r"ORD[A-Za-z0-9]{1,80}", remote_id):
        return None
    remote = fetch_remote_order(remote_id)
    return remote if isinstance(remote, dict) and str(remote.get("id") or "") == remote_id and _remote_matches_order(remote, order, seller_id) else None


def remote_payments(remote: dict[str, Any]) -> list[dict[str, Any]]:
    """Orders API uses transactions.payments; tolerate the earlier list shape."""
    transactions = remote.get("transactions") or {}
    groups = [transactions] if isinstance(transactions, dict) else transactions
    if not isinstance(groups, list):
        return []
    result: list[dict[str, Any]] = []
    for transaction in groups:
        if not isinstance(transaction, dict):
            continue
        entries = transaction.get("payments", transaction)
        if isinstance(entries, dict):
            entries = [entries]
        if isinstance(entries, list):
            result.extend(entry for entry in entries if isinstance(entry, dict))
    return result


def payment_status_from_remote(remote: dict[str, Any]) -> str:
    approved = {"approved", "accredited", "processed"}
    rejected = {"rejected", "cancelled", "canceled", "failed", "expired"}
    refunded = {"refunded", "charged_back"}

    def values(resource: dict[str, Any]) -> set[str]:
        return {str(resource.get(key) or "").lower() for key in ("status", "status_detail")}

    order_values = values(remote)
    # Terminal order states take precedence over an earlier accredited payment.
    if order_values & refunded:
        return "refunded"
    if order_values & rejected:
        return "rejected"
    # Partial refunds require separate accounting, not a full stock reversal.
    if "partially_refunded" in order_values:
        return "pending"
    entries = [values(entry) for entry in remote_payments(remote)]
    if entries and all(entry & refunded for entry in entries):
        return "refunded"
    if order_values & approved:
        return "approved"
    if entries and all(entry & approved and not entry & (rejected | refunded) for entry in entries):
        return "approved"
    if entries and all(entry & rejected for entry in entries):
        return "rejected"
    return "pending"


def verify_webhook_signature(x_signature: str, x_request_id: str, data_id: str, secret: str) -> bool:
    if not x_signature or not secret:
        return False
    parts: dict[str, str] = {}
    for fragment in x_signature.split(","):
        key, separator, value = fragment.partition("=")
        if separator:
            parts[key.strip()] = value.strip()
    timestamp = parts.get("ts")
    signature = parts.get("v1")
    if not timestamp or not timestamp.isdecimal() or not signature or not re.fullmatch(r"[a-fA-F0-9]{64}", signature):
        return False
    manifest = ""
    if data_id:
        manifest += f"id:{data_id};"
    if x_request_id:
        manifest += f"request-id:{x_request_id};"
    manifest += f"ts:{timestamp};"
    expected = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def _request_json(
    path: str,
    method: str,
    token: str,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "BYSG-Store/1.0",
    }
    headers.update(extra_headers or {})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{MERCADO_PAGO_API}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15, context=verified_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # Provider error bodies may echo private request data. Never put them in
        # browser responses or application logs.
        raise PaymentProviderError(f"Mercado Pago respondió HTTP {error.code}. Revisá la configuración del pago.") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise PaymentProviderError("No se pudo completar la comunicación con Mercado Pago. Revisá la conexión.") from error
