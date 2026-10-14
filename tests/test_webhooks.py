from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sandbox_server as sandbox

database = sandbox.store.database
payments = sandbox.store.payments
webhooks = sandbox.store.webhooks
SELLER = "3738474084"
BUYER = "3737970722"
ORDER_ID = "ORDTSTLOCAL123"
SECRET = "synthetic-webhook-secret-not-real-123456"


class RemoteStatusTests(unittest.TestCase):
    def test_failure_diagnostics_never_include_exception_text_or_credentials(self):
        reported = []
        processor = webhooks.OrderNotifications(on_failed=reported.append)
        private_error = RuntimeError("secret-token private@example.com")
        processor.report_failure(private_error)
        provider_error = payments.PaymentProviderError("private provider response")
        provider_error.__cause__ = urllib.error.HTTPError("https://private?token=secret", 403, "secret", {}, None)
        processor.report_failure(provider_error)
        self.assertEqual(reported, ["internal_error", "provider_http_403"])

    def test_current_orders_api_shape_is_supported(self):
        for provider, expected in [("processed", "approved"), ("failed", "rejected"), ("created", "pending")]:
            with self.subTest(provider=provider):
                remote = {"transactions": {"payments": [{"status": provider}]}}
                self.assertEqual(payments.payment_status_from_remote(remote), expected)

    def test_legacy_shape_and_malformed_entries_are_safe(self):
        self.assertEqual(payments.payment_status_from_remote({"transactions": [{"payments": [{"status": "approved"}]}]}), "approved")
        for transactions in [None, "bad", ["bad", None], {"payments": "bad"}]:
            with self.subTest(transactions=transactions):
                self.assertEqual(payments.payment_status_from_remote({"transactions": transactions}), "pending")

    def test_terminal_order_state_wins_over_old_accredited_payment(self):
        for status, expected in [("refunded", "refunded"), ("cancelled", "rejected"), ("partially_refunded", "pending")]:
            remote = {"status": status, "transactions": {"payments": [{"status": "processed", "status_detail": "accredited"}]}}
            self.assertEqual(payments.payment_status_from_remote(remote), expected)

    def test_mixed_incomplete_payments_cannot_approve_order(self):
        remote = {"status": "created", "transactions": {"payments": [{"status": "processed"}, {"status": "pending"}]}}
        self.assertEqual(payments.payment_status_from_remote(remote), "pending")

    def test_test_order_and_resource_path_are_fail_closed(self):
        with patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago_prueba"}), patch.object(payments, "_request_json") as fetch:
            for identifier in ["ORDLIVE123", "../payments/1", ORDER_ID + "?token=x", "", ORDER_ID + "/extra"]:
                with self.subTest(identifier=identifier), self.assertRaises(payments.PaymentProviderError):
                    payments.fetch_remote_order(identifier)
            fetch.assert_not_called()

    def test_bad_signature_is_rejected_without_unicode_comparison_error(self):
        for signature in ["ts=abc,v1=" + "a" * 64, "ts=123,v1=" + "ñ" * 64, "ts=123,v1=bad"]:
            self.assertFalse(payments.verify_webhook_signature(signature, "req", ORDER_ID, SECRET))


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago_prueba", "SMTP_HOST": "", "SMTP_FROM": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.scratch = tempfile.TemporaryDirectory(prefix="bysg-notification-test-")
        self.addCleanup(self.scratch.cleanup)
        self.database_patch = patch.object(database, "DATABASE_PATH", Path(self.scratch.name) / "tests.sqlite3")
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        database.initialize_database()
        product = database.catalog()["products"][0]
        self.category = product["category_code"]
        self.order = database.create_order({
            "customer": {"name": "Persona Prueba", "email": f"testuser{BUYER}@testuser.com", "phone": "12345678"},
            "delivery_method": "pickup",
            "items": [{"variant_id": product["variant_id"], "size_label": "XL", "quantity": 1}],
        }, provider="mercado_pago_prueba")
        database.attach_provider_order(self.order["public_token"], ORDER_ID)
        self.processor = webhooks.OrderNotifications(
            secret=lambda: SECRET,
            test_seller_id=SELLER,
            on_approved=lambda order: sandbox.store.mailer.queue_order_event(order, "payment_approved", "http://127.0.0.1"),
        )

    def payload(self, event_id="1", **changes):
        return {"id": event_id, "type": "order", "user_id": SELLER, "data": {"id": ORDER_ID}, **changes}

    def remote(self, status="processed", **changes):
        return {
            "id": ORDER_ID, "user_id": SELLER,
            "external_reference": self.order["public_token"],
            "currency": "ARS", "total_amount": "25000.00", "total_paid_amount": "25000.00",
            "status": status,
            "transactions": {"payments": [{"id": "PAYTEST123", "status": status}]},
            "client_token": "sensitive-provider-client-token",
            "payer": {"email": "private-payer@example.com"},
            **changes,
        }

    def current(self):
        return database.get_order(self.order["public_token"], include_private=True)

    def stock(self):
        return next(row for row in database.stock_status() if row["code"] == self.category)

    def process(self, remote=None, payload=None):
        with patch.object(payments, "fetch_remote_order", return_value=remote or self.remote()):
            return self.processor.process(ORDER_ID, payload or self.payload())

    def test_verified_approval_updates_order_stock_balance_and_single_email(self):
        self.assertEqual(self.process(), {"status": "processed"})
        self.assertEqual((self.current()["payment_status"], self.current()["order_status"]), ("approved", "paid"))
        self.assertEqual((self.stock()["reserved"], self.stock()["sold"]), (0, 1))
        self.assertEqual(database.dashboard()["bauti_account"]["balance_cents"], 2500000)
        self.assertEqual(len(database.pending_emails()), 1)
        with patch.object(payments, "fetch_remote_order") as fetch:
            self.assertEqual(self.processor.process(ORDER_ID, self.payload()), {"status": "duplicate"})
            fetch.assert_not_called()
        self.process(payload=self.payload("2"))
        self.assertEqual(self.stock()["sold"], 1)
        self.assertEqual(database.dashboard()["bauti_account"]["balance_cents"], 2500000)
        self.assertEqual(len(database.pending_emails()), 1)

    def test_provider_failure_is_retryable_for_same_event(self):
        with patch.object(payments, "fetch_remote_order", side_effect=payments.PaymentProviderError("temporary")), self.assertRaises(payments.PaymentProviderError):
            self.processor.process(ORDER_ID, self.payload())
        self.assertEqual(self.current()["payment_status"], "pending")
        self.assertEqual(self.process(), {"status": "processed"})
        self.assertEqual(self.current()["payment_status"], "approved")

    def test_queue_failure_after_payment_can_retry_without_double_sale(self):
        with patch.object(sandbox.store.mailer, "queue_order_event", side_effect=RuntimeError("temporary")), self.assertRaises(RuntimeError):
            self.process()
        self.assertEqual(self.process(), {"status": "processed"})
        self.assertEqual(self.stock()["sold"], 1)
        self.assertEqual(database.dashboard()["bauti_account"]["balance_cents"], 2500000)
        self.assertEqual(len(database.pending_emails()), 1)

    def test_pending_and_rejected_states_respect_stock(self):
        self.process(self.remote("created"))
        self.assertEqual((self.current()["payment_status"], self.stock()["reserved"]), ("pending", 1))
        self.process(self.remote("failed"), self.payload("2"))
        self.assertEqual((self.current()["payment_status"], self.stock()["reserved"], self.stock()["sold"]), ("rejected", 0, 0))

    def test_late_failure_does_not_undo_paid_order(self):
        self.process()
        self.process(self.remote("failed"), self.payload("2"))
        self.assertEqual(self.current()["payment_status"], "approved")
        self.assertEqual(self.stock()["sold"], 1)

    def test_retry_success_after_rejection_reacquires_stock_without_negative_reservation(self):
        self.process(self.remote("failed"))
        self.process(payload=self.payload("2"))
        self.assertEqual((self.stock()["reserved"], self.stock()["sold"]), (0, 1))

    def test_refund_and_late_approval_cannot_double_credit_account(self):
        self.process()
        self.process(self.remote("refunded"), self.payload("2"))
        self.process(payload=self.payload("3"))
        self.assertEqual(self.current()["payment_status"], "refunded")
        self.assertEqual((self.stock()["reserved"], self.stock()["sold"]), (0, 0))
        self.assertEqual(database.dashboard()["bauti_account"]["balance_cents"], 0)

    def test_invalid_notification_body_and_real_seller_are_rejected_before_fetch(self):
        invalid = [self.payload(data={"id": "ORDOTHER"}), self.payload(user_id="999"), self.payload(id=""), self.payload(data=None)]
        with patch.object(payments, "fetch_remote_order") as fetch:
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(webhooks.WebhookRejected):
                    self.processor.process(ORDER_ID, payload)
            fetch.assert_not_called()

    def test_remote_order_identity_reference_amount_currency_and_paid_total_must_match(self):
        invalid = [
            {"id": "ORDOTHER"}, {"user_id": "999"}, {"external_reference": "unknown"},
            {"total_amount": "24999.00"}, {"total_amount": "NaN"}, {"currency": "USD"},
            {"total_paid_amount": "1.00"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(webhooks.WebhookRejected):
                self.process(self.remote(**changes))
            self.assertEqual(self.current()["payment_status"], "pending")

    def test_configured_seller_must_match_remote_order(self):
        processor = webhooks.OrderNotifications(expected_seller_id=SELLER)
        for event_id, remote_seller in [("seller-missing", None), ("seller-other", "999")]:
            with self.subTest(remote_seller=remote_seller), \
                 patch.object(payments, "fetch_remote_order", return_value=self.remote(user_id=remote_seller)), \
                 self.assertRaises(webhooks.WebhookRejected):
                processor.process(ORDER_ID, self.payload(event_id))
            self.assertEqual(self.current()["payment_status"], "pending")
        with patch.object(payments, "fetch_remote_order", return_value=self.remote()):
            self.assertEqual(processor.process(ORDER_ID, self.payload("seller-valid")), {"status": "processed"})
        self.assertEqual(self.current()["payment_status"], "approved")

    def test_mismatched_local_provider_order_is_rejected(self):
        with database.transaction() as connection:
            connection.execute(
                "UPDATE orders SET provider_order_id = ? WHERE public_token = ?",
                ("ORDTSTOTHER", self.order["public_token"]),
            )
        with self.assertRaises(webhooks.WebhookRejected):
            self.process()

    def test_only_minimal_non_sensitive_payment_summary_is_persisted(self):
        self.process()
        with database.connect() as connection:
            stored = connection.execute("SELECT raw_payload FROM payments").fetchone()[0]
            receipt = connection.execute("SELECT payload FROM webhook_events").fetchone()[0]
        for secret in ["sensitive-provider-client-token", "private-payer@example.com", SECRET]:
            self.assertNotIn(secret, stored + receipt)


class CallbackHTTPTests(NotificationTests):
    def setUp(self):
        super().setUp()
        handler = type("CallbackTestHandler", (webhooks.CallbackOnlyHandler,), {"notifications": self.processor})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def request(self, method="POST", path=None, payload=None, signature=True, extra_headers=None):
        path = path or f"{webhooks.CALLBACK_PATH}?data.id={ORDER_ID}&type=order"
        headers = {"Content-Type": "application/json", "x-request-id": "test-request"}
        if signature:
            manifest = f"id:{ORDER_ID};request-id:test-request;ts:1742505638683;"
            digest = hmac.new(SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()
            headers["x-signature"] = f"ts=1742505638683,v1={digest}"
        headers.update(extra_headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=3)
        connection.request(method, path, json.dumps(payload or self.payload()), headers)
        response = connection.getresponse()
        status, body = response.status, response.read().decode()
        connection.close()
        return status, json.loads(body)

    def test_callback_listener_has_no_files_store_admin_or_configuration_routes(self):
        for path in ["/", "/local/pagos", "/admin.html", "/api/orders/unknown", "/.env", "/app/database.py"]:
            for method in ["GET", "POST"]:
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.request(method, path)[0], 404)

    def test_unsigned_or_ambiguous_id_cannot_call_provider(self):
        with patch.object(payments, "fetch_remote_order") as fetch:
            self.assertEqual(self.request(signature=False)[0], 401)
            self.assertEqual(self.request(path=f"{webhooks.CALLBACK_PATH}?data.id={ORDER_ID}&data.id={ORDER_ID}")[0], 401)
            fetch.assert_not_called()

    def test_signed_http_callback_marks_order_paid(self):
        with patch.object(payments, "fetch_remote_order", return_value=self.remote()):
            self.assertEqual(self.request(), (200, {"status": "processed"}))
        self.assertEqual(self.current()["order_status"], "paid")

    def test_simulator_without_envelope_id_uses_signed_delivery_id(self):
        payload = self.payload()
        payload.pop("id")
        with patch.object(payments, "fetch_remote_order", return_value=self.remote()) as fetch:
            self.assertEqual(self.request(payload=payload), (200, {"status": "processed"}))
            self.assertEqual(self.request(payload=payload), (200, {"status": "duplicate"}))
            fetch.assert_called_once_with(ORDER_ID)
        self.assertEqual(self.current()["order_status"], "paid")

    def test_simulator_illustrative_paid_data_is_not_payment_evidence(self):
        payload = self.payload()
        payload.pop("id")
        payload["data"].update({"status": "processed", "total_paid_amount": 100000, "external_reference": "ext_ref_1234"})
        with patch.object(payments, "fetch_remote_order", return_value=self.remote("pending")):
            self.assertEqual(self.request(payload=payload)[0], 200)
        self.assertEqual(self.current()["payment_status"], "pending")

    def test_http_failure_is_not_acknowledged_and_same_event_can_retry(self):
        with patch.object(payments, "fetch_remote_order", side_effect=RuntimeError("sensitive-error")):
            code, body = self.request()
            self.assertEqual(code, 503)
            self.assertNotIn("sensitive-error", json.dumps(body))
        with patch.object(payments, "fetch_remote_order", return_value=self.remote()):
            self.assertEqual(self.request()[0], 200)

    def test_non_json_and_oversized_body_are_rejected_before_provider(self):
        with patch.object(payments, "fetch_remote_order") as fetch:
            self.assertEqual(self.request(extra_headers={"Content-Type": "text/plain"})[0], 400)
            self.assertEqual(self.request(extra_headers={"Content-Length": str(webhooks.MAX_BODY_BYTES + 1)})[0], 400)
            fetch.assert_not_called()


class SandboxNotificationSettingsTests(unittest.TestCase):
    def test_settings_are_memory_only_and_disabled_without_separate_listener(self):
        with patch.dict(os.environ, {}, clear=False):
            session = sandbox.SandboxSession(SELLER, BUYER)
            session.connected = True
            with self.assertRaises(ValueError):
                session.configure_notifications(SECRET)
            session.webhook_port = 4182
            session.configure_notifications(SECRET)
            self.assertTrue(session.status()["webhook_receiver_ready"])
            self.assertNotIn(SECRET, json.dumps(session.status()))
            self.assertNotIn("MP_WEBHOOK_SECRET", os.environ)
            session.disconnect()
            self.assertEqual(session.webhook_secret, "")
            self.assertFalse(session.status()["webhook_receiver_ready"])


class SandboxResumeTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="bysg-pagos-prueba-")
        self.addCleanup(self.scratch.cleanup)
        self.source = Path(self.scratch.name) / "pruebas.sqlite3"
        self.destination = Path(self.scratch.name) / "snapshot.sqlite3"
        patcher = patch.object(database, "DATABASE_PATH", self.source)
        patcher.start()
        self.addCleanup(patcher.stop)
        database.initialize_database()
        product = database.catalog()["products"][0]
        self.order = database.create_order({
            "customer": {"name": "Persona Prueba", "email": f"testuser{BUYER}@testuser.com", "phone": "12345678"},
            "delivery_method": "pickup",
            "items": [{"variant_id": product["variant_id"], "size_label": "XL", "quantity": 1}],
        }, provider="mercado_pago_prueba")
        database.attach_provider_order(self.order["public_token"], ORDER_ID)

    def test_snapshot_preserves_order_and_reservation_without_modifying_original(self):
        sandbox.copy_test_database(self.source, self.destination, f"testuser{BUYER}@testuser.com")
        with patch.object(database, "DATABASE_PATH", self.destination):
            self.assertEqual(database.get_order(self.order["public_token"])["provider_order_id"], ORDER_ID)
            self.assertEqual(sum(row["reserved"] for row in database.stock_status()), 1)
        self.assertEqual(database.get_order(self.order["public_token"])["payment_status"], "pending")

    def test_customer_or_non_test_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            sandbox.copy_test_database(self.source, self.destination, "other@testuser.com")
        with database.connect() as connection:
            connection.execute("UPDATE orders SET provider='mercado_pago'")
        with self.assertRaises(ValueError):
            sandbox.copy_test_database(self.source, self.destination, f"testuser{BUYER}@testuser.com")

    def test_regular_store_database_is_never_accepted(self):
        sandbox.copy_test_database(self.source, self.destination, f"testuser{BUYER}@testuser.com")
        with self.assertRaises(ValueError):
            sandbox.copy_test_database(self.destination, self.source, f"testuser{BUYER}@testuser.com")


if __name__ == "__main__":
    unittest.main()
