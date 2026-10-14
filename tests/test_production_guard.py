from __future__ import annotations

import os
import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class CapturedPost:
    def __init__(self, path: str, payload: dict | None = None):
        self.path = path
        self.body = payload or {}
        self.status = None
        self.response = None
        self.read_count = 0

    def _json(self, status, payload):
        self.status = status
        self.response = payload

    def _require_rate(self, *args):
        pass

    def _require_same_origin(self):
        pass

    def _read_json(self):
        self.read_count += 1
        return self.body

    def _server_error(self, error):
        raise error


class CapturedGet:
    path = "/api/health"

    def __init__(self):
        self.status = None
        self.response = None

    def _json(self, status, payload):
        self.status = status
        self.response = payload

    def _server_error(self, error):
        raise error


class ProductionPaymentGuardTests(unittest.TestCase):
    REQUEST_ID = "123e4567-e89b-42d3-a456-426614174000"

    def post(self, path: str, payload: dict | None = None) -> CapturedPost:
        handler = CapturedPost(path, payload)
        server.BYSGHandler.do_POST(handler)
        return handler

    def test_publication_requires_expected_seller_and_positive_prices(self):
        legal = {
            "BYSG_LEGAL_NAME": "Titular Prueba", "BYSG_LEGAL_CUIT": "123",
            "BYSG_CONTACT_EMAIL": "titular@example.test", "BYSG_BUSINESS_ADDRESS": "Calle 123",
            "MP_ACCESS_TOKEN": "test-token", "MP_WEBHOOK_SECRET": "test-secret",
            "BYSG_PRODUCTION": "true", "BYSG_PAYMENT_MODE": "mercado_pago",
        }
        cases = [("", 100, False), ("seller-name", 100, False), ("12345", 0, False), ("12345", 100, True)]
        with patch.object(server, "BASE_URL", "https://shop.example.test"), \
             patch.object(server, "SESSION_SECRET", "s" * 32), \
             patch.object(server, "ADMIN_PASSWORD", "secure-password"), \
             patch.object(server, "BAUTI_CODE", "secure-code"), \
             patch.object(server.mailer, "smtp_configured", return_value=True):
            for seller_id, price_cents, expected in cases:
                with self.subTest(seller_id=seller_id, price_cents=price_cents), \
                     patch.dict(os.environ, {**legal, "MP_SELLER_ID": seller_id}), \
                     patch.object(server.database, "category_financials", return_value=[
                         {"is_sample_price": 0, "price_cents": price_cents},
                     ]):
                    readiness = server.publication_readiness()
                    checks = {check["key"]: check["ready"] for check in readiness["checks"]}
                    self.assertIs(checks["mp_seller"], bool(seller_id == "12345"))
                    self.assertIs(checks["final_prices"], bool(price_cents > 0))
                    self.assertIs(server.real_payments_enabled(), expected)

    def test_production_checkout_rejects_mock_before_creating_an_order(self):
        for setting in ("true", "unrecognized-value"):
            with self.subTest(setting=setting), \
                 patch.dict(os.environ, {"BYSG_PRODUCTION": setting, "BYSG_PAYMENT_MODE": "mock"}), \
                 patch.object(server, "publication_readiness", return_value={"ready": True}) as readiness, \
                 patch.object(server.database, "create_order") as create_order:
                response = self.post("/api/checkout/orders")
                self.assertEqual(response.status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(response.read_count, 0)
                create_order.assert_not_called()
                readiness.assert_not_called()

    def test_production_checkout_rejects_incomplete_readiness_before_creating_an_order(self):
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "yes", "BYSG_PAYMENT_MODE": "mercado_pago"}), \
             patch.object(server, "publication_readiness", return_value={"ready": False}), \
             patch.object(server.database, "get_existing_order_for_request", return_value=None), \
             patch.object(server.database, "create_order") as create_order:
            response = self.post("/api/checkout/orders", {"client_request_id": self.REQUEST_ID})
        self.assertEqual(response.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(response.read_count, 1)
        create_order.assert_not_called()

    def test_real_checkout_requires_explicit_production_mode(self):
        for payment_mode in ("mercado_pago", "unexpected_mode"):
            with self.subTest(payment_mode=payment_mode), \
                 patch.dict(os.environ, {"BYSG_PRODUCTION": "false", "BYSG_PAYMENT_MODE": payment_mode}), \
                 patch.object(server, "publication_readiness", return_value={"ready": True}) as readiness, \
                 patch.object(server.database, "create_order") as create_order, \
                 patch.object(server.payments, "create_checkout") as provider_checkout:
                response = self.post("/api/checkout/orders")
                self.assertEqual(response.status, HTTPStatus.SERVICE_UNAVAILABLE)
                self.assertEqual(response.read_count, 0)
                create_order.assert_not_called()
                provider_checkout.assert_not_called()
                readiness.assert_not_called()

    def test_production_cannot_approve_a_mock_payment(self):
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "true", "BYSG_PAYMENT_MODE": "mock"}), \
             patch.object(server.database, "apply_payment_status") as apply_status:
            response = self.post("/api/mock/payments", {"status": "approved", "public_token": "sample"})
        self.assertEqual(response.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(response.read_count, 0)
        apply_status.assert_not_called()

    def test_local_mock_and_sandbox_checkout_remain_available(self):
        order = {"public_token": "local-token", "order_number": "BYSG-TEST-1", "payment_status": "pending"}
        for payment_mode in ("mock", "mercado_pago_prueba"):
            with self.subTest(payment_mode=payment_mode), \
                 patch.dict(os.environ, {"BYSG_PRODUCTION": "false", "BYSG_PAYMENT_MODE": payment_mode}), \
                 patch.object(server, "publication_readiness") as readiness, \
                 patch.object(server.database, "create_or_get_order", return_value=(order, True)) as create_order, \
                 patch.object(server.payments, "create_checkout", return_value={
                     "provider_order_id": "ORDTST123", "checkout_url": "https://example.test/checkout", "provider": payment_mode,
                 }), \
                 patch.object(server.database, "attach_provider_order"), \
                 patch.object(server.mailer, "queue_order_event"), \
                 patch.object(server.mailer, "deliver_pending"):
                response = self.post("/api/checkout/orders", {"items": [], "client_request_id": self.REQUEST_ID})
                self.assertEqual(response.status, HTTPStatus.CREATED)
                create_order.assert_called_once_with({"items": []}, payment_mode, self.REQUEST_ID)
                readiness.assert_not_called()

    def test_local_mock_approval_remains_available(self):
        order = {"public_token": "local-token", "payment_status": "approved"}
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "false", "BYSG_PAYMENT_MODE": "mock"}), \
             patch.object(server.database, "apply_payment_status", return_value=order) as apply_status, \
             patch.object(server.mailer, "queue_order_event"), \
             patch.object(server.mailer, "deliver_pending"):
            response = self.post("/api/mock/payments", {"status": "approved", "public_token": "local-token"})
        self.assertEqual(response.status, HTTPStatus.OK)
        self.assertEqual(response.response, {"order": order})
        apply_status.assert_called_once()

    def test_ready_production_checkout_is_allowed(self):
        order = {"public_token": "production-token", "order_number": "BYSG-TEST-2", "payment_status": "pending"}
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "true", "BYSG_PAYMENT_MODE": "mercado_pago", "MP_SELLER_ID": "12345"}), \
             patch.object(server, "publication_readiness", return_value={"ready": True}) as readiness, \
             patch.object(server.database, "get_existing_order_for_request", return_value=None), \
             patch.object(server.payments, "verify_seller_identity", return_value=True), \
             patch.object(server.database, "create_or_get_order", return_value=(order, True)) as create_order, \
             patch.object(server.payments, "create_checkout", return_value={
                 "provider_order_id": "ORD123", "checkout_url": "https://example.test/checkout", "provider": "mercado_pago",
             }), \
             patch.object(server.database, "attach_provider_order"), \
             patch.object(server.mailer, "queue_order_event"), \
             patch.object(server.mailer, "deliver_pending"):
            response = self.post("/api/checkout/orders", {"items": [], "client_request_id": self.REQUEST_ID})
        self.assertEqual(response.status, HTTPStatus.CREATED)
        create_order.assert_called_once_with({"items": []}, "mercado_pago", self.REQUEST_ID)
        readiness.assert_called_once_with()

    def test_seller_identity_failure_does_not_reserve_stock(self):
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "true", "BYSG_PAYMENT_MODE": "mercado_pago", "MP_SELLER_ID": "12345"}), \
             patch.object(server, "publication_readiness", return_value={"ready": True}), \
             patch.object(server.database, "get_existing_order_for_request", return_value=None), \
             patch.object(server.payments, "verify_seller_identity", return_value=False) as verify, \
             patch.object(server.database, "create_or_get_order") as create_order:
            response = self.post("/api/checkout/orders", {"client_request_id": self.REQUEST_ID})
        self.assertEqual(response.status, HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(response.read_count, 1)
        verify.assert_called_once_with("12345")
        create_order.assert_not_called()

    def test_existing_request_is_returned_without_identity_call_or_second_order(self):
        order = {"public_token": "production-token", "order_number": "BYSG-TEST-2", "payment_status": "pending"}
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "true", "BYSG_PAYMENT_MODE": "mercado_pago", "MP_SELLER_ID": "12345"}), \
             patch.object(server, "publication_readiness", return_value={"ready": True}), \
             patch.object(server.database, "get_existing_order_for_request", return_value=order) as lookup, \
             patch.object(server.payments, "verify_seller_identity") as verify, \
             patch.object(server.database, "create_or_get_order") as create_order, \
             patch.object(server.payments, "create_checkout") as create_checkout:
            response = self.post("/api/checkout/orders", {"items": [], "client_request_id": self.REQUEST_ID})
        self.assertEqual(response.status, HTTPStatus.ACCEPTED)
        self.assertNotIn("checkout_url", response.response)
        lookup.assert_called_once_with({"items": []}, "mercado_pago", self.REQUEST_ID)
        verify.assert_not_called()
        create_order.assert_not_called()
        create_checkout.assert_not_called()

    def test_existing_request_survives_readiness_change(self):
        order = {"public_token": "production-token", "order_number": "BYSG-TEST-2", "payment_status": "pending"}
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "true", "BYSG_PAYMENT_MODE": "mercado_pago"}), \
             patch.object(server, "publication_readiness", return_value={"ready": False}), \
             patch.object(server.database, "get_existing_order_for_request", return_value=order), \
             patch.object(server.payments, "verify_seller_identity") as verify:
            response = self.post("/api/checkout/orders", {"items": [], "client_request_id": self.REQUEST_ID})
        self.assertEqual(response.status, HTTPStatus.ACCEPTED)
        self.assertEqual(response.response["public_token"], "production-token")
        verify.assert_not_called()

    def test_ambiguous_provider_failure_keeps_order_pending_without_checkout_url(self):
        order = {"public_token": "local-token", "order_number": "BYSG-TEST-3", "payment_status": "pending"}
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "false", "BYSG_PAYMENT_MODE": "mercado_pago_prueba"}), \
             patch.object(server.database, "create_or_get_order", return_value=(order, True)), \
             patch.object(server.payments, "create_checkout", side_effect=server.payments.PaymentProviderError("provider unavailable")), \
             patch.object(server.database, "apply_payment_status") as apply_status, \
             patch.object(server.mailer, "queue_order_event"):
            response = self.post("/api/checkout/orders", {"items": [], "client_request_id": self.REQUEST_ID})
        self.assertEqual(response.status, HTTPStatus.ACCEPTED)
        self.assertEqual(response.response["public_token"], "local-token")
        self.assertNotIn("checkout_url", response.response)
        apply_status.assert_not_called()

    def test_repeated_checkout_request_never_reposts_to_provider(self):
        order = {"public_token": "local-token", "order_number": "BYSG-TEST-3", "payment_status": "pending"}
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "false", "BYSG_PAYMENT_MODE": "mercado_pago_prueba"}), \
             patch.object(server.database, "create_or_get_order", return_value=(order, False)) as create_order, \
             patch.object(server.payments, "create_checkout") as create_checkout:
            response = self.post("/api/checkout/orders", {"items": [], "client_request_id": self.REQUEST_ID})
        self.assertEqual(response.status, HTTPStatus.ACCEPTED)
        self.assertNotIn("checkout_url", response.response)
        create_order.assert_called_once_with({"items": []}, "mercado_pago_prueba", self.REQUEST_ID)
        create_checkout.assert_not_called()

    def test_health_reports_real_payments_only_when_production_is_ready(self):
        cases = (
            ("false", "mock", True, False),
            ("false", "mercado_pago", True, False),
            ("true", "mock", True, False),
            ("true", "mercado_pago", False, False),
            ("true", "mercado_pago", True, True),
        )
        for production, payment_mode, ready, expected in cases:
            with self.subTest(production=production, payment_mode=payment_mode, ready=ready), \
                 patch.dict(os.environ, {"BYSG_PRODUCTION": production, "BYSG_PAYMENT_MODE": payment_mode}), \
                 patch.object(server, "publication_readiness", return_value={"ready": ready}):
                response = CapturedGet()
                server.BYSGHandler.do_GET(response)
                self.assertEqual(response.status, HTTPStatus.OK)
                self.assertIs(response.response["real_payments_enabled"], expected)
                self.assertNotIn("MP_ACCESS_TOKEN", response.response)
                self.assertNotIn("MP_WEBHOOK_SECRET", response.response)


if __name__ == "__main__":
    unittest.main()
