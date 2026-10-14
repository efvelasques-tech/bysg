from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import payments  # noqa: E402
import server  # noqa: E402
import database  # noqa: E402


TOKEN = "test-access-token-never-log"
PUBLIC_TOKEN = "local-token-123456789012345"
CHECKOUT_URL = "https://www.mercadopago.com.ar/checkout/v1/redirect?order_id=ORD123"


def local_order() -> dict:
    return {
        "public_token": PUBLIC_TOKEN, "order_number": "BYSG-TEST-1",
        "provider": "mercado_pago", "provider_order_id": None,
        "payment_status": "pending", "order_status": "payment_pending",
        "customer_name": "Persona Prueba", "customer_email": "buyer@example.test",
        "currency": "ARS", "total_cents": 2500000,
        "created_at": datetime(2026, 10, 8, 12, tzinfo=timezone.utc).isoformat(),
        "items": [{"sku": "BYSG-TEST", "title": "Remera de prueba", "size_label": "M", "quantity": 1, "unit_price_cents": 2500000}],
    }


def remote_order(**changes) -> dict:
    result = {
        "id": "ORD123", "user_id": "12345", "external_reference": PUBLIC_TOKEN,
        "currency": "ARS", "total_amount": "25000.00", "total_paid_amount": "0.00",
        "status": "created", "transactions": {"payments": [{"status": "pending"}]},
        "checkout_url": CHECKOUT_URL,
    }
    result.update(changes)
    return result


class CapturedPost:
    path = "/api/checkout/recover"

    def __init__(self):
        self.status = None
        self.response = None

    def _json(self, status, payload):
        self.status = status
        self.response = payload

    def _require_rate(self, *args):
        pass

    def _require_same_origin(self):
        pass

    def _read_json(self):
        return {"public_token": PUBLIC_TOKEN}

    def _recover_checkout(self, *args):
        return server.BYSGHandler._recover_checkout(self, *args)

    def _server_error(self, error):
        raise error


class SellerIdentityTests(unittest.TestCase):
    def test_real_seller_requires_matching_id_argentina_and_no_test_tag(self):
        cases = [
            ({"id": 12345, "site_id": "MLA", "tags": ["normal"]}, True),
            ({"id": 99999, "site_id": "MLA", "tags": ["normal"]}, False),
            ({"id": 12345, "site_id": "MLB", "tags": ["normal"]}, False),
            ({"id": 12345, "site_id": "MLA", "tags": ["test_user"]}, False),
            ({"id": 12345, "site_id": "MLA"}, False),
        ]
        for profile, expected in cases:
            with self.subTest(profile=profile), patch.dict(os.environ, {"MP_ACCESS_TOKEN": TOKEN}), \
                 patch.object(payments.urllib.request, "build_opener") as build:
                response = io.BytesIO(json.dumps(profile).encode())
                response.status = 200
                build.return_value.open.return_value = response
                self.assertIs(payments.verify_seller_identity("12345"), expected)
            self.assertEqual(build.return_value.open.call_args.args[0].full_url, payments.MERCADO_LIBRE_IDENTITY_API)
            self.assertTrue(any(isinstance(arg, payments._NoRedirect) for arg in build.call_args.args))

    def test_identity_lookup_errors_fail_closed_without_token_in_message(self):
        with patch.dict(os.environ, {"MP_ACCESS_TOKEN": TOKEN}), \
             patch.object(payments.urllib.request, "build_opener") as build, \
             self.assertRaises(payments.PaymentProviderError) as caught:
            build.return_value.open.side_effect = urllib.error.URLError(TOKEN)
            payments.verify_seller_identity("12345")
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_identity_lookup_rejects_other_success_status(self):
        response = io.BytesIO(b'{"id":12345,"site_id":"MLA","tags":[]}')
        response.status = 204
        with patch.dict(os.environ, {"MP_ACCESS_TOKEN": TOKEN}), \
             patch.object(payments.urllib.request, "build_opener") as build, \
             self.assertRaises(payments.PaymentProviderError):
            build.return_value.open.return_value = response
            payments.verify_seller_identity("12345")


class ProviderRecoveryTests(unittest.TestCase):
    def test_invalid_create_reply_does_not_offer_a_checkout(self):
        examples = [
            {"id": "ORD123", "user_id": "12345", "checkout_url": "https://evil.example/checkout"},
            {"id": "ORD123", "user_id": "99999", "checkout_url": CHECKOUT_URL},
            {"id": "bad-id", "user_id": "12345", "checkout_url": CHECKOUT_URL},
            {"id": "ORDTST123", "user_id": "12345", "checkout_url": CHECKOUT_URL},
        ]
        for reply in examples:
            with self.subTest(reply=reply), patch.dict(os.environ, {"MP_ACCESS_TOKEN": TOKEN, "MP_SELLER_ID": "12345", "BYSG_PAYMENT_MODE": "mercado_pago"}), \
                 patch.object(payments, "_request_json", return_value=reply), \
                 self.assertRaises(payments.PaymentProviderError):
                payments.create_checkout(local_order(), "https://shop.example.test")

    def test_searches_bounded_exact_reference_then_gets_and_validates_single_order(self):
        search = {"data": [{"id": "ORD123"}], "paging": {"total": 1}}
        with patch.dict(os.environ, {"MP_ACCESS_TOKEN": TOKEN, "BYSG_PAYMENT_MODE": "mercado_pago"}), \
             patch.object(payments, "_request_json", return_value=search) as request, \
             patch.object(payments, "fetch_remote_order", return_value=remote_order()) as fetch:
            result = payments.recover_remote_order(local_order(), "12345")
        self.assertEqual(result["id"], "ORD123")
        self.assertEqual(fetch.call_args.args, ("ORD123",))
        path = urlparse(request.call_args.args[0])
        query = parse_qs(path.query)
        self.assertEqual(path.path, "/v1/orders")
        self.assertEqual(query["external_reference"], [PUBLIC_TOKEN])
        self.assertEqual(query["limit"], ["10"])
        self.assertEqual(query["offset"], ["0"])
        self.assertEqual(request.call_args.kwargs, {"method": "GET", "token": TOKEN})

    def test_empty_ambiguous_or_mismatched_search_cannot_link(self):
        searches = [
            {"data": [], "paging": {"total": 0}},
            {"data": [{"id": "ORD123"}], "paging": {"total": 2}},
            {"data": [{"id": "ORD123"}, {"id": "ORD456"}], "paging": {"total": 2}},
        ]
        for search in searches:
            with self.subTest(search=search), patch.dict(os.environ, {"MP_ACCESS_TOKEN": TOKEN}), \
                 patch.object(payments, "_request_json", return_value=search), \
                 patch.object(payments, "fetch_remote_order") as fetch:
                self.assertIsNone(payments.recover_remote_order(local_order(), "12345"))
            fetch.assert_not_called()
        mismatches = [
            {"user_id": "99999"}, {"external_reference": "other"},
            {"currency": "USD"}, {"total_amount": "25001.00"},
            {"id": "ORDTST123"},
            {"status": "processed", "total_paid_amount": "1.00"},
        ]
        for change in mismatches:
            with self.subTest(change=change), patch.dict(os.environ, {"MP_ACCESS_TOKEN": TOKEN}), \
                 patch.object(payments, "_request_json", return_value={"data": [{"id": "ORD123"}], "paging": {"total": 1}}), \
                 patch.object(payments, "fetch_remote_order", return_value=remote_order(**change)):
                self.assertIsNone(payments.recover_remote_order(local_order(), "12345"))

    def test_test_order_recovery_requires_test_order_id_and_session_seller(self):
        order = {**local_order(), "provider": "mercado_pago_prueba", "customer_email": "testuser99@testuser.com"}
        search = {"data": [{"id": "ORDTST123"}], "paging": {"total": 1}}
        remote = remote_order(id="ORDTST123", checkout_url="https://www.mercadopago.com.ar/checkout?order_id=ORDTST123")
        with patch.dict(os.environ, {"MP_ACCESS_TOKEN": TOKEN, "BYSG_PAYMENT_MODE": "mercado_pago_prueba"}), \
             patch.object(payments, "_request_json", return_value=search), \
             patch.object(payments, "fetch_remote_order", return_value=remote):
            self.assertEqual(payments.recover_remote_order(order, "12345")["id"], "ORDTST123")
            self.assertIsNone(payments.recover_remote_order(order, "99999"))
            remote["id"] = "ORD123"
            self.assertIsNone(payments.recover_remote_order(order, "12345"))

    def test_rejects_unofficial_checkout_links(self):
        links = [
            "http://www.mercadopago.com.ar/checkout",
            "https://www.mercadopago.com.ar.evil.example/checkout",
            "https://user@www.mercadopago.com.ar/checkout",
            "https://www.mercadopago.com.ar:444/checkout",
            "https://www.mercadopago.com.ar/account",
            "https://www.mercadopago.com.ar/checkout#fragment",
            "https://www.mercadopago.com.ar/checkout?client_token=secret",
            f"https://www.mercadopago.com.ar/checkout?access_token={TOKEN}",
        ]
        for link in links:
            with self.subTest(link=link):
                self.assertIsNone(payments.official_checkout_url(link, TOKEN))
        self.assertEqual(payments.official_checkout_url(CHECKOUT_URL, TOKEN), CHECKOUT_URL)


class RecoveryRouteTests(unittest.TestCase):
    def post(self) -> CapturedPost:
        handler = CapturedPost()
        server.BYSGHandler.do_POST(handler)
        return handler

    def test_unique_verified_pending_order_links_once_and_returns_official_checkout(self):
        with patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago", "BYSG_PRODUCTION": "true", "MP_SELLER_ID": "12345"}), \
             patch.object(server, "real_payments_enabled", return_value=True), \
             patch.object(server.database, "get_order", return_value=local_order()), \
             patch.object(server.payments, "recover_remote_order", return_value=remote_order()), \
             patch.object(server.database, "attach_provider_order") as attach:
            result = self.post()
        self.assertEqual(result.status, HTTPStatus.OK)
        self.assertEqual(result.response["checkout_url"], CHECKOUT_URL)
        attach.assert_called_once_with(PUBLIC_TOKEN, "ORD123")

    def test_unverified_remote_stays_pending_without_checkout_or_stock_change(self):
        with patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago", "BYSG_PRODUCTION": "true", "MP_SELLER_ID": "12345"}), \
             patch.object(server, "real_payments_enabled", return_value=True), \
             patch.object(server.database, "get_order", return_value=local_order()), \
             patch.object(server.payments, "recover_remote_order", return_value=None), \
             patch.object(server.database, "attach_provider_order") as attach, \
             patch.object(server.database, "apply_payment_status") as apply_status:
            result = self.post()
        self.assertEqual(result.status, HTTPStatus.ACCEPTED)
        self.assertNotIn("checkout_url", result.response)
        attach.assert_not_called()
        apply_status.assert_not_called()

    def test_sandbox_uses_locked_session_seller_and_only_its_test_buyer(self):
        order = {**local_order(), "provider": "mercado_pago_prueba", "customer_email": "testuser99@testuser.com"}
        remote = remote_order(id="ORDTST123", checkout_url="https://www.mercadopago.com.ar/checkout?order_id=ORDTST123")
        session = SimpleNamespace(connected=True, seller_id="12345", buyer_email="testuser99@testuser.com", lock=threading.RLock())
        with tempfile.TemporaryDirectory(prefix="bysg-pagos-prueba-") as scratch, \
             patch.object(database, "DATABASE_PATH", Path(scratch) / "pruebas.sqlite3"), \
             patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago_prueba", "MP_ACCESS_TOKEN": TOKEN, "MP_SELLER_ID": "99999"}), \
             patch.object(database, "get_order", return_value=order), \
             patch.object(payments, "recover_remote_order", return_value=remote) as recover, \
             patch.object(database, "attach_provider_order") as attach:
            handler = CapturedPost()
            handler.session = session
            server.BYSGHandler.do_POST(handler)
            self.assertEqual(handler.status, HTTPStatus.OK)
            self.assertEqual(handler.response["checkout_url"], remote["checkout_url"])
            recover.assert_called_once_with(order, "12345")
            attach.assert_called_once_with(PUBLIC_TOKEN, "ORDTST123")

            order["customer_email"] = "another@testuser.com"
            handler = CapturedPost()
            handler.session = session
            server.BYSGHandler.do_POST(handler)
            self.assertEqual(handler.status, HTTPStatus.CONFLICT)
            self.assertEqual(recover.call_count, 1)

    def test_sandbox_disconnected_or_nonisolated_database_never_queries_provider(self):
        session = SimpleNamespace(connected=False, seller_id="12345", buyer_email="testuser99@testuser.com", lock=threading.RLock())
        with patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago_prueba"}), \
             patch.object(payments, "recover_remote_order") as recover:
            handler = CapturedPost()
            handler.session = session
            server.BYSGHandler.do_POST(handler)
            self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
            session.connected = True
            handler = CapturedPost()
            handler.session = session
            server.BYSGHandler.do_POST(handler)
            self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
            recover.assert_not_called()

    def test_sandbox_approved_remote_does_not_mutate_payment_status(self):
        order = {**local_order(), "provider": "mercado_pago_prueba", "customer_email": "testuser99@testuser.com"}
        remote = remote_order(id="ORDTST123", status="processed", total_paid_amount="25000.00")
        session = SimpleNamespace(connected=True, seller_id="12345", buyer_email="testuser99@testuser.com", lock=threading.RLock())
        with tempfile.TemporaryDirectory(prefix="bysg-pagos-prueba-") as scratch, \
             patch.object(database, "DATABASE_PATH", Path(scratch) / "pruebas.sqlite3"), \
             patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago_prueba"}), \
             patch.object(database, "get_order", return_value=order), \
             patch.object(payments, "recover_remote_order", return_value=remote), \
             patch.object(database, "attach_provider_order"), \
             patch.object(database, "apply_payment_status") as apply_status:
            handler = CapturedPost()
            handler.session = session
            server.BYSGHandler.do_POST(handler)
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.response["payment_status"], "pending")
        self.assertEqual(handler.response["provider_payment_status"], "approved")
        self.assertNotIn("checkout_url", handler.response)
        apply_status.assert_not_called()


class IdempotentLookupTests(unittest.TestCase):
    def test_retry_lookup_preserves_the_existing_reservation(self):
        request_id = "123e4567-e89b-42d3-a456-426614174000"
        payload = {
            "customer": {"name": "Persona Prueba", "email": "buyer@example.test", "phone": "3515555555"},
            "delivery_method": "pickup", "items": [],
        }
        with tempfile.TemporaryDirectory() as scratch, patch.object(database, "DATABASE_PATH", Path(scratch) / "test.sqlite3"):
            database.initialize_database(reset=True)
            item = database.catalog()["products"][0]
            payload["items"] = [{"variant_id": item["variant_id"], "size_label": "S", "quantity": 1}]
            first, created = database.create_or_get_order(payload, "mock", request_id)
            self.assertTrue(created)
            existing = database.get_existing_order_for_request(payload, "mock", request_id)
            self.assertEqual(existing["public_token"], first["public_token"])
            self.assertEqual(database.stock_status()[0]["reserved"], 1)
            with self.assertRaises(ValueError):
                database.get_existing_order_for_request({**payload, "notes": "changed"}, "mock", request_id)
            self.assertIsNone(database.get_existing_order_for_request(payload, "mock", "123e4567-e89b-42d3-a456-426614174001"))
            self.assertEqual(database.stock_status()[0]["reserved"], 1)


if __name__ == "__main__":
    unittest.main()
