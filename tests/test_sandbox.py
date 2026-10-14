from __future__ import annotations

import http.client
import io
import json
import os
import sqlite3
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import sandbox_server as sandbox

payments = sandbox.store.payments
import secure_http
TOKEN = "TEST-" + "synthetic-not-a-real-secret-" * 2
SELLER = "3738474084"
BUYER = "3737970722"
EMAIL = f"testuser{BUYER}@testuser.com"


class IdentityTests(unittest.TestCase):
    def profile(self, **changes):
        return {"id": int(SELLER), "tags": ["test_user"], "site_id": "MLA", **changes}

    def verify(self, profile):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(profile).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(sandbox.urllib.request, "build_opener", return_value=opener):
            sandbox.verify_test_token(TOKEN, SELLER)
        return opener.open.call_args

    def test_identity_is_checked_with_header_and_read_only_request(self):
        args = self.verify(self.profile())
        request = args.args[0]
        self.assertEqual(request.full_url, "https://api.mercadolibre.com/users/me")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(args.kwargs["timeout"], 15)

    def test_real_account_wrong_seller_and_wrong_country_are_rejected(self):
        for profile in [self.profile(tags=[]), self.profile(id=123), self.profile(site_id="MLB"), []]:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                self.verify(profile)

    def test_malformed_token_never_reaches_network(self):
        with patch.object(sandbox.urllib.request, "build_opener") as opener:
            for token in ["", "password", "TEST-short", TOKEN + "\nextra"]:
                with self.assertRaises(ValueError) as error:
                    sandbox.verify_test_token(token, SELLER)
                if token:
                    self.assertNotIn(token, str(error.exception))
            opener.assert_not_called()

    def test_redirects_cannot_forward_token(self):
        self.assertIsNone(sandbox.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_authorization_errors_are_distinct_and_do_not_echo_token(self):
        error = urllib.error.HTTPError("https://api.mercadolibre.com/users/me", 403, TOKEN, {}, io.BytesIO(TOKEN.encode()))
        opener = MagicMock()
        opener.open.side_effect = error
        with patch.object(sandbox.urllib.request, "build_opener", return_value=opener), self.assertRaises(ValueError) as caught:
            sandbox.verify_test_token(TOKEN, SELLER)
        self.assertIn("no autorizó", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))


class TLSTrustTests(unittest.TestCase):
    def test_empty_macos_trust_uses_existing_system_bundle(self):
        context = MagicMock()
        context.get_ca_certs.return_value = []
        with patch.object(secure_http.ssl, "create_default_context", return_value=context), patch.object(secure_http.sys, "platform", "darwin"), patch.object(Path, "is_file", return_value=True):
            self.assertIs(secure_http.verified_ssl_context(), context)
        context.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/cert.pem")

    def test_existing_trust_configuration_is_preserved(self):
        context = MagicMock()
        context.get_ca_certs.return_value = [{"subject": "existing CA"}]
        with patch.object(secure_http.ssl, "create_default_context", return_value=context), patch.object(secure_http.sys, "platform", "darwin"):
            secure_http.verified_ssl_context()
        context.load_verify_locations.assert_not_called()

    def test_certificate_and_hostname_verification_remain_enabled(self):
        context = secure_http.verified_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.session = sandbox.SandboxSession(SELLER, BUYER)

    def test_token_is_memory_only_and_status_does_not_expose_it(self):
        with patch.object(sandbox, "verify_test_token"), patch.object(Path, "write_text") as write_text, patch.object(Path, "write_bytes") as write_bytes:
            self.session.connect(TOKEN, EMAIL)
        self.assertTrue(self.session.connected)
        self.assertEqual(os.environ["MP_ACCESS_TOKEN"], TOKEN)
        self.assertNotIn(TOKEN, json.dumps(self.session.status()))
        write_text.assert_not_called()
        write_bytes.assert_not_called()
        self.session.disconnect()
        self.assertNotIn("MP_ACCESS_TOKEN", os.environ)
        self.assertEqual(payments.payment_mode(), "sandbox_sin_clave")

    def test_invalid_replacement_disconnects_and_personal_email_is_rejected(self):
        with patch.object(sandbox, "verify_test_token"):
            self.session.connect(TOKEN, EMAIL)
            with self.assertRaises(ValueError):
                self.session.connect(TOKEN, "person@example.com")
        self.assertFalse(self.session.connected)
        self.assertNotIn("MP_ACCESS_TOKEN", os.environ)

    def test_inherited_token_is_removed_before_enabling_session(self):
        os.environ["MP_ACCESS_TOKEN"] = "inherited-token"
        sandbox.SandboxSession(SELLER, BUYER)
        self.assertNotIn("MP_ACCESS_TOKEN", os.environ)


class PendingOrderCheckTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.scratch = tempfile.TemporaryDirectory(prefix="bysg-pagos-prueba-")
        self.addCleanup(self.scratch.cleanup)
        self.db_path = Path(self.scratch.name) / "pruebas.sqlite3"
        database_path = patch.object(sandbox.store.database, "DATABASE_PATH", self.db_path)
        database_path.start()
        self.addCleanup(database_path.stop)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("""CREATE TABLE orders (
                public_token TEXT, order_number TEXT, provider TEXT,
                provider_order_id TEXT, customer_email TEXT, currency TEXT,
                total_cents INTEGER, payment_status TEXT, created_at TEXT)""")
            connection.execute(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("local-token", "BYSG-TEST-1", "mercado_pago_prueba", "ORDTST123", EMAIL, "ARS", 2500000, "pending", "2026-10-04T22:44:00+00:00"),
            )
        self.session = sandbox.SandboxSession(SELLER, BUYER)
        with patch.object(sandbox, "verify_test_token"):
            self.session.connect(TOKEN, EMAIL)

    def remote(self, **changes):
        return {
            "id": "ORDTST123", "user_id": SELLER, "external_reference": "local-token",
            "currency": "ARS", "total_amount": "25000.00", "total_paid_amount": "25000.00",
            "status": "processed", "transactions": {"payments": [{"status": "approved"}]}, **changes,
        }

    def test_approved_remote_order_is_reported_but_local_order_remains_pending(self):
        with patch.object(payments, "fetch_remote_order", return_value=self.remote()) as fetch, patch.object(sandbox.store.database, "apply_payment_status") as update:
            result = sandbox.check_pending_test_order(self.session)
        fetch.assert_called_once_with("ORDTST123")
        update.assert_not_called()
        self.assertEqual(result["result"], "verified")
        self.assertEqual(result["provider_payment_status"], "approved")
        self.assertEqual(result["local_payment_status"], "pending")
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn("local-token", json.dumps(result))
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT payment_status FROM orders").fetchone()[0], "pending")

    def test_explicit_reconciliation_uses_exact_lookup_and_sanitized_summary(self):
        updated = {"payment_status": "approved"}
        with patch.object(payments, "fetch_remote_order", return_value=self.remote()) as fetch, \
             patch.object(sandbox.store.database, "apply_payment_status", return_value=updated) as apply:
            result = sandbox.reconcile_pending_test_order(self.session)
        fetch.assert_called_once_with("ORDTST123")
        apply.assert_called_once()
        self.assertEqual(apply.call_args.args[:3], ("local-token", "approved", "ORDTST123"))
        self.assertEqual(apply.call_args.args[3], {
            "source": "manual_sandbox_reconciliation", "provider_order_id": "ORDTST123",
            "payment_status": "approved", "currency": "ARS", "total_amount_cents": 2500000,
        })
        self.assertEqual(result["result"], "reconciled")
        self.assertEqual(result["local_payment_status"], "approved")
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn("local-token", json.dumps(result))

    def test_reconciliation_does_not_write_for_unapproved_or_mismatched_remote(self):
        remotes = [
            self.pending_remote(), self.remote(id="ORDTST999"), self.remote(user_id="other-seller"),
            self.remote(external_reference="other-order"), self.remote(currency="USD"),
            self.remote(total_amount="25001.00"), self.remote(total_paid_amount="1.00"),
        ]
        for remote in remotes:
            with self.subTest(remote=remote), patch.object(payments, "fetch_remote_order", return_value=remote), \
                 patch.object(sandbox.store.database, "apply_payment_status") as apply:
                result = sandbox.reconcile_pending_test_order(self.session)
            self.assertIn(result["result"], {"mismatch", "not_approved"})
            apply.assert_not_called()

    def test_reconciliation_requires_one_pending_order_connected_session_and_temp_copy(self):
        with patch.object(payments, "fetch_remote_order") as fetch, patch.object(sandbox.store.database, "apply_payment_status") as apply:
            self.session.disconnect()
            with self.assertRaises(ValueError):
                sandbox.reconcile_pending_test_order(self.session)
            fetch.assert_not_called()
            apply.assert_not_called()
        with patch.object(sandbox, "verify_test_token"):
            self.session.connect(TOKEN, EMAIL)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("INSERT INTO orders SELECT * FROM orders")
        with patch.object(payments, "fetch_remote_order") as fetch, self.assertRaises(ValueError):
            sandbox.reconcile_pending_test_order(self.session)
        fetch.assert_not_called()
        other_path = Path(self.scratch.name).parent / "not-an-isolated-copy.sqlite3"
        with patch.object(sandbox.store.database, "DATABASE_PATH", other_path), patch.object(payments, "fetch_remote_order") as fetch, self.assertRaises(ValueError):
            sandbox.reconcile_pending_test_order(self.session)
        fetch.assert_not_called()

    def test_reconciliation_rechecks_local_pending_order_before_writing(self):
        def changed_remote(_order_id):
            with sqlite3.connect(self.db_path) as connection:
                connection.execute("UPDATE orders SET payment_status = 'rejected'")
            return self.remote()

        with patch.object(payments, "fetch_remote_order", side_effect=changed_remote), \
             patch.object(sandbox.store.database, "apply_payment_status") as apply, \
             self.assertRaises(ValueError):
            sandbox.reconcile_pending_test_order(self.session)
        apply.assert_not_called()

    def test_identity_amount_paid_total_and_unique_pending_order_are_required(self):
        mismatches = [
            self.remote(id="ORDTST999"), self.remote(user_id="other-seller"),
            self.remote(external_reference="other-order"), self.remote(currency="USD"),
            self.remote(total_amount="25001.00"), self.remote(total_paid_amount="1.00"),
        ]
        for remote in mismatches:
            with self.subTest(remote=remote), patch.object(payments, "fetch_remote_order", return_value=remote):
                self.assertEqual(sandbox.check_pending_test_order(self.session)["result"], "mismatch")
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("INSERT INTO orders SELECT * FROM orders")
        with patch.object(payments, "fetch_remote_order") as fetch, self.assertRaises(ValueError):
            sandbox.check_pending_test_order(self.session)
        fetch.assert_not_called()

    def not_found_by_id(self):
        failure = payments.PaymentProviderError(TOKEN)
        failure.__cause__ = urllib.error.HTTPError("https://api.mercadopago.com/v1/orders/ORDTST123", 404, TOKEN, {}, io.BytesIO(TOKEN.encode()))
        return failure

    def test_provider_404_searches_exact_reference_in_bounded_dates_without_writes(self):
        search = {"data": [self.remote()], "paging": {"total": "1", "limit": "10", "offset": "0"}}
        with patch.object(payments, "fetch_remote_order", side_effect=self.not_found_by_id()), patch.object(payments, "_request_json", return_value=search) as request:
            result = sandbox.check_pending_test_order(self.session)
        self.assertEqual(result["result"], "verified")
        self.assertEqual(result["source"], "search")
        self.assertTrue(result["id_match"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["provider_payment_status"], "approved")
        path = request.call_args.args[0]
        self.assertEqual(urlparse(path).path, "/v1/orders")
        query = parse_qs(urlparse(path).query)
        self.assertEqual(query["external_reference"], ["local-token"])
        self.assertEqual(query["limit"], ["10"])
        self.assertEqual(query["offset"], ["0"])
        start = datetime.fromisoformat(query["begin_date"][0].replace("Z", "+00:00"))
        end = datetime.fromisoformat(query["end_date"][0].replace("Z", "+00:00"))
        self.assertEqual((end - start).total_seconds(), 2 * 24 * 60 * 60)
        self.assertLess(start, datetime(2026, 10, 4, 22, 44, tzinfo=timezone.utc))
        self.assertEqual(request.call_args.kwargs, {"method": "GET", "token": TOKEN})
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn("local-token", json.dumps(result))
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT payment_status FROM orders").fetchone()[0], "pending")

    def test_search_empty_or_unauthorized_is_inconclusive_not_absence(self):
        with patch.object(payments, "fetch_remote_order", side_effect=self.not_found_by_id()), patch.object(payments, "_request_json", return_value={"data": [], "paging": {"total": "0"}}):
            result = sandbox.check_pending_test_order(self.session)
        self.assertEqual(result["result"], "search_empty")
        self.assertEqual(result["candidate_count"], 0)
        self.assertIn("no demuestra", result["message"])
        failure = payments.PaymentProviderError(TOKEN)
        failure.__cause__ = urllib.error.HTTPError("https://api.mercadopago.com/v1/orders", 401, TOKEN, {}, io.BytesIO(TOKEN.encode()))
        with patch.object(payments, "fetch_remote_order", side_effect=self.not_found_by_id()), patch.object(payments, "_request_json", side_effect=failure):
            result = sandbox.check_pending_test_order(self.session)
        self.assertEqual(result["result"], "search_unavailable")
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_search_other_id_and_multiple_results_cannot_verify_local_order(self):
        with patch.object(payments, "fetch_remote_order", side_effect=self.not_found_by_id()), patch.object(payments, "_request_json", return_value={"data": [self.remote(id="ORDTST999")], "paging": {"total": 1}}):
            result = sandbox.check_pending_test_order(self.session)
        self.assertEqual(result["result"], "different_order_id")
        self.assertFalse(result["id_match"])
        with patch.object(payments, "fetch_remote_order", side_effect=self.not_found_by_id()), patch.object(payments, "_request_json", return_value={"data": [self.remote()], "paging": {"total": 2}}):
            result = sandbox.check_pending_test_order(self.session)
        self.assertEqual(result["result"], "search_ambiguous")
        self.assertFalse(result["id_match"])

    def pending_remote(self, **changes):
        return self.remote(status="action_required", status_detail="waiting_for_payment",
                           transactions={"payments": [{"status": "pending"}]}, **changes)

    def test_checkout_recovery_uses_exact_get_and_returns_only_official_url(self):
        url = "https://www.mercadopago.com.ar/checkout/v1/redirect?order_id=ORDTST123"
        remote = self.pending_remote(checkout_url=url, client_token="private-client-secret",
                                     payer={"email": EMAIL}, access_token=TOKEN)
        with patch.object(payments, "_request_json", return_value=remote) as request, \
             patch.object(sandbox.store.database, "create_order") as create, \
             patch.object(sandbox.store.database, "apply_payment_status") as update:
            result = sandbox.recover_pending_test_checkout(self.session)
        self.assertEqual(request.call_args.args[0], "/v1/orders/ORDTST123")
        self.assertEqual(request.call_args.kwargs, {"method": "GET", "token": TOKEN})
        self.assertEqual(result["result"], "checkout_available")
        self.assertEqual(result["checkout_url"], url)
        self.assertTrue(result["checkout_url_present"])
        self.assertIsNone(result["app_id_match"])
        self.assertNotIn("client_token", result)
        self.assertNotIn("payer", result)
        self.assertNotIn(TOKEN, json.dumps(result))
        create.assert_not_called()
        update.assert_not_called()
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT payment_status FROM orders").fetchone()[0], "pending")

    def test_checkout_recovery_missing_url_reports_only_safe_diagnostic(self):
        remote = self.pending_remote(app_id=123456)
        with patch.dict(os.environ, {"MP_APP_ID": "123456"}), patch.object(payments, "fetch_remote_order", return_value=remote):
            result = sandbox.recover_pending_test_checkout(self.session)
        self.assertEqual(result["result"], "missing_checkout_url")
        self.assertEqual(result["provider_order_status"], "action_required")
        self.assertEqual(result["provider_status_detail"], "waiting_for_payment")
        self.assertFalse(result["checkout_url_present"])
        self.assertTrue(result["app_id_match"])
        self.assertNotIn("checkout_url", result)
        self.assertNotIn("local-token", json.dumps(result))
        remote["status_detail"] = "private-client-secret"
        remote["client_token"] = "private-client-secret"
        with patch.object(payments, "fetch_remote_order", return_value=remote):
            result = sandbox.recover_pending_test_checkout(self.session)
        self.assertIsNone(result["provider_status_detail"])
        self.assertNotIn("private-client-secret", json.dumps(result))

    def test_checkout_recovery_rejects_identity_app_and_payment_mismatches(self):
        examples = [
            self.pending_remote(id="ORDTST999", checkout_url="https://www.mercadopago.com.ar/checkout"),
            self.pending_remote(user_id="other-seller", checkout_url="https://www.mercadopago.com.ar/checkout"),
            self.pending_remote(external_reference="other-order", checkout_url="https://www.mercadopago.com.ar/checkout"),
            self.pending_remote(currency="USD", checkout_url="https://www.mercadopago.com.ar/checkout"),
            self.pending_remote(total_amount="25001.00", checkout_url="https://www.mercadopago.com.ar/checkout"),
            self.pending_remote(app_id=999, checkout_url="https://www.mercadopago.com.ar/checkout"),
        ]
        with patch.dict(os.environ, {"MP_APP_ID": "123456"}):
            for remote in examples:
                with self.subTest(remote=remote), patch.object(payments, "fetch_remote_order", return_value=remote):
                    result = sandbox.recover_pending_test_checkout(self.session)
                self.assertEqual(result["result"], "mismatch")
                self.assertNotIn("checkout_url", result)

    def test_checkout_recovery_rejects_unsafe_urls_and_completed_orders(self):
        unsafe = ["http://www.mercadopago.com.ar/checkout", "https://evil.example/checkout",
                  "https://www.mercadopago.com.ar.evil.example/checkout",
                  "https://user@www.mercadopago.com.ar/checkout",
                  "https://www.mercadopago.com.ar:444/checkout",
                  "https://www.mercadopago.com.ar/checkout#fragment",
                  "https://www.mercadopago.com.ar/account",
                  "https://www.mercadopago.com.ar/checkout?client_token=secret",
                  f"https://www.mercadopago.com.ar/checkout?access_token={TOKEN}"]
        for url in unsafe:
            with self.subTest(url=url), patch.object(payments, "fetch_remote_order", return_value=self.pending_remote(checkout_url=url)):
                result = sandbox.recover_pending_test_checkout(self.session)
            self.assertEqual(result["result"], "invalid_checkout_url")
            self.assertNotIn("checkout_url", result)
        with patch.object(payments, "fetch_remote_order", return_value=self.remote(checkout_url="https://www.mercadopago.com.ar/checkout")):
            result = sandbox.recover_pending_test_checkout(self.session)
        self.assertEqual(result["result"], "not_pending")
        self.assertNotIn("checkout_url", result)

    def test_checkout_recovery_requires_one_pending_local_order(self):
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("INSERT INTO orders SELECT * FROM orders")
        with patch.object(payments, "fetch_remote_order") as fetch, self.assertRaises(ValueError):
            sandbox.recover_pending_test_checkout(self.session)
        fetch.assert_not_called()


class ReconciliationDatabaseTests(unittest.TestCase):
    def test_verified_approval_updates_only_isolated_order_and_stock_once(self):
        with tempfile.TemporaryDirectory(prefix="bysg-pagos-prueba-") as scratch, \
             patch.dict(os.environ, {}, clear=False), \
             patch.object(sandbox.store.database, "DATABASE_PATH", Path(scratch) / "pruebas.sqlite3"):
            database = sandbox.store.database
            database.initialize_database()
            now = database.utc_now()
            with database.transaction() as connection:
                variant = connection.execute("SELECT id FROM variants WHERE sku = ?", ("BYSG-REM-D01-NEG-PRE",)).fetchone()
                self.assertIsNotNone(variant)
                cursor = connection.execute(
                    """INSERT INTO orders(public_token, order_number, customer_name, customer_email,
                       customer_phone, delivery_method, subtotal_cents, total_cents,
                       provider, provider_order_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("local-token", "BYSG-TEST-1", "Persona Prueba", EMAIL, "123456789",
                     "pickup", 2500000, 2500000, "mercado_pago_prueba", "ORDTST123", now, now),
                )
                order_id = cursor.lastrowid
                connection.execute(
                    """INSERT INTO order_items(order_id, variant_id, sku, title, category_code,
                       design_number, size_label, quantity, unit_price_cents, line_total_cents)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (order_id, variant[0], "BYSG-REM-D01-NEG-PRE", "Remera de prueba", "REM", 1, "XL", 1, 2500000, 2500000),
                )
                connection.execute("UPDATE categories SET reserved = 1 WHERE code = 'REM'")
                connection.execute(
                    """INSERT INTO payments(order_id, provider, status, amount_cents, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (order_id, "mercado_pago_prueba", "pending", 2500000, now, now),
                )
            session = sandbox.SandboxSession(SELLER, BUYER)
            with patch.object(sandbox, "verify_test_token"):
                session.connect(TOKEN, EMAIL)
            remote = {
                "id": "ORDTST123", "user_id": SELLER, "external_reference": "local-token",
                "currency": "ARS", "total_amount": "25000.00", "total_paid_amount": "25000.00",
                "status": "processed", "transactions": {"payments": [{"status": "approved"}]},
                "client_token": "never-persist-this",
            }
            with patch.object(payments, "fetch_remote_order", return_value=remote):
                result = sandbox.reconcile_pending_test_order(session)
                with self.assertRaises(ValueError):
                    sandbox.reconcile_pending_test_order(session)
            self.assertEqual(result["result"], "reconciled")
            with database.connect() as connection:
                order = connection.execute("SELECT payment_status, order_status FROM orders WHERE id = ?", (order_id,)).fetchone()
                stock = connection.execute("SELECT reserved, sold FROM categories WHERE code = 'REM'").fetchone()
                payment = connection.execute("SELECT status, raw_payload FROM payments WHERE order_id = ?", (order_id,)).fetchone()
                ledgers = connection.execute("SELECT COUNT(*) FROM stock_ledger WHERE order_id = ? AND event_type = 'payment_approved'", (order_id,)).fetchone()[0]
            self.assertEqual(tuple(order), ("approved", "paid"))
            self.assertEqual(tuple(stock), (0, 1))
            self.assertEqual(payment["status"], "approved")
            self.assertNotIn("never-persist-this", payment["raw_payload"])
            self.assertNotIn(TOKEN, payment["raw_payload"])
            self.assertEqual(ledgers, 1)
            session.disconnect()


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.session = sandbox.SandboxSession(SELLER, BUYER)
        self.handler = type("TestHandler", (sandbox.SandboxHandler,), {"session": self.session})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.host = f"127.0.0.1:{self.httpd.server_port}"
        self.origin = f"http://{self.host}"
        self.handler.origin = self.origin
        base_url = patch.object(sandbox.store, "BASE_URL", self.origin)
        base_url.start()
        self.addCleanup(base_url.stop)
        sandbox.store.RATE_BUCKETS.clear()
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=3)
        body = json.dumps(payload) if payload is not None else None
        connection.request(method, path, body, {"Host": self.host, **(headers or {})})
        response = connection.getresponse()
        result = response.status, response.getheaders(), response.read().decode()
        connection.close()
        return result

    def private_headers(self):
        return {"Origin": self.origin, "Cookie": f"bysg_sandbox={self.session.cookie}", "X-BYSG-CSRF": self.session.csrf, "Content-Type": "application/json"}

    def test_private_page_has_no_cache_nonce_and_http_only_cookie(self):
        code, headers, body = self.request("GET", "/local/pagos")
        self.assertEqual(code, 200)
        values = dict(headers)
        self.assertEqual(values["Cache-Control"], "no-store")
        self.assertEqual(values["Referrer-Policy"], "no-referrer")
        self.assertIn("HttpOnly; SameSite=Strict", values["Set-Cookie"])
        self.assertIn(f"'nonce-{self.session.csrf}'", values["Content-Security-Policy"])
        self.assertIn(f'nonce="{self.session.csrf}"', body)
        self.assertNotIn("{{CSRF}}", body)
        self.assertNotIn(TOKEN, body)
        self.assertIn('id="checkOrderButton"', body)
        self.assertIn('id="reconcileOrderButton"', body)
        self.assertIn('id="recoverCheckoutButton"', body)
        self.assertIn("api('/api/local/pagos/conciliar-pedido',{})", body)
        self.assertIn("api('/api/local/pagos/recuperar-checkout',{})", body)
        self.assertIn("error.status===404?'La recuperación de enlaces no está disponible", body)
        self.assertNotIn("https://www.mercadopago.com.ar/checkout", body)

    def test_order_check_requires_private_session_and_returns_only_safe_result(self):
        diagnostic = {"result": "not_found", "message": "No se encontró con esta clave."}
        with patch.object(sandbox, "check_pending_test_order", return_value=diagnostic) as check:
            self.assertEqual(self.request("POST", "/api/local/pagos/comprobar-pedido", {})[0], 403)
            check.assert_not_called()
            code, _, body = self.request("POST", "/api/local/pagos/comprobar-pedido", {}, self.private_headers())
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body), diagnostic)
            self.assertNotIn(TOKEN, body)
            check.assert_called_once_with(self.session)

    def test_checkout_recovery_route_requires_private_post_and_empty_body(self):
        result = {"result": "missing_checkout_url", "checkout_url_present": False}
        with patch.object(sandbox, "recover_pending_test_checkout", return_value=result) as recover:
            self.assertEqual(self.request("GET", "/api/local/pagos/recuperar-checkout")[0], 404)
            self.assertEqual(self.request("POST", "/api/local/pagos/recuperar-checkout", {})[0], 403)
            self.assertEqual(self.request("POST", "/api/local/pagos/recuperar-checkout", {"order_id": "ORDTST999"}, self.private_headers())[0], 409)
            recover.assert_not_called()
            code, _, body = self.request("POST", "/api/local/pagos/recuperar-checkout", {}, self.private_headers())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), result)
        recover.assert_called_once_with(self.session)

    def test_reconciliation_route_requires_private_post_and_empty_body(self):
        result = {"result": "reconciled", "order_number": "BYSG-TEST-1", "local_payment_status": "approved"}
        with patch.object(sandbox, "reconcile_pending_test_order", return_value=result) as reconcile:
            self.assertEqual(self.request("GET", "/api/local/pagos/conciliar-pedido")[0], 404)
            self.assertEqual(self.request("POST", "/api/local/pagos/conciliar-pedido", {})[0], 403)
            self.assertEqual(self.request("POST", "/api/local/pagos/conciliar-pedido", {"order_id": "ORDTST999"}, self.private_headers())[0], 409)
            reconcile.assert_not_called()
            code, _, body = self.request("POST", "/api/local/pagos/conciliar-pedido", {}, self.private_headers())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), result)
        reconcile.assert_called_once_with(self.session)

    def test_remote_origin_rebinding_and_cross_site_fetch_are_rejected(self):
        for headers in [{"Host": "other.example"}, {"Origin": "https://other.example"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/api/local/pagos", headers=headers)[0], 403)

    def test_private_post_needs_origin_cookie_csrf_and_json(self):
        correct = self.private_headers()
        for missing in correct:
            headers = {key: value for key, value in correct.items() if key != missing}
            with self.subTest(missing=missing), patch.object(sandbox, "verify_test_token") as verify:
                code, _, body = self.request("POST", "/api/local/pagos/conectar", {"access_token": TOKEN, "buyer_email": EMAIL}, headers)
                self.assertEqual(code, 403)
                self.assertNotIn(TOKEN, body)
                verify.assert_not_called()

    def test_connect_then_disconnect_returns_no_secret_and_never_creates_order(self):
        with patch.object(sandbox, "verify_test_token"), patch.object(sandbox.store.database, "create_order") as create_order:
            code, _, body = self.request("POST", "/api/local/pagos/conectar", {"access_token": TOKEN, "buyer_email": EMAIL}, self.private_headers())
            self.assertEqual(code, 200)
            self.assertTrue(json.loads(body)["connected"])
            self.assertNotIn(TOKEN, body)
            self.assertNotIn(TOKEN, self.request("GET", "/api/local/pagos")[2])
            create_order.assert_not_called()
            code, _, body = self.request("POST", "/api/local/pagos/desconectar", {}, self.private_headers())
            self.assertEqual(code, 200)
            self.assertFalse(json.loads(body)["connected"])
            self.assertNotIn("MP_ACCESS_TOKEN", os.environ)

    def test_checkout_is_disabled_until_connected_and_rejects_real_email(self):
        self.assertEqual(self.request("POST", "/api/checkout/orders", {})[0], 409)
        self.session.connected = True
        with patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago_prueba"}), patch.object(sandbox.store.database, "create_order") as create_order:
            code, _, body = self.request("POST", "/api/checkout/orders", {"customer": {"email": "person@example.com"}}, {"Origin": self.origin})
            self.assertEqual(code, 400)
            create_order.assert_not_called()

    def test_static_private_files_and_mutating_admin_actions_are_blocked(self):
        for path in ["/sandbox_server.py", "/.env", "/backups/secret.json", "/outputs/Finanzas.xlsx", "/app/sandbox-config.html", "/tests/test_sandbox.py"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", "/api/admin/financials", {})[0], 403)
        self.assertEqual(self.request("GET", "/muestra-web/checkout.html")[0], 200)


class CheckoutSafetyTests(unittest.TestCase):
    def order(self):
        return {"public_token": "local-order", "order_number": "BYSG-TEST", "total_cents": 2500000, "customer_email": EMAIL, "customer_name": "Persona Prueba", "items": [{"sku": "BYSG-TEST", "title": "Remera de prueba", "size_label": "M", "quantity": 1, "unit_price_cents": 2500000}]}

    def test_test_mode_omits_local_return_urls_and_requires_test_order(self):
        with patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago_prueba", "MP_ACCESS_TOKEN": TOKEN}), patch.object(payments, "_request_json", return_value={"id": "ORDTST123", "checkout_url": "https://www.mercadopago.com.ar/checkout"}) as request:
            result = payments.create_checkout(self.order(), "http://127.0.0.1:4181")
            self.assertEqual(result["provider_order_id"], "ORDTST123")
            self.assertNotIn("online", request.call_args.kwargs["payload"]["config"])
            request.return_value = {"id": "ORD123", "checkout_url": "https://www.mercadopago.com.ar/checkout"}
            with self.assertRaises(payments.PaymentProviderError):
                payments.create_checkout(self.order(), "http://127.0.0.1:4181")

    def test_regular_mode_keeps_existing_return_urls(self):
        with patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago", "MP_ACCESS_TOKEN": TOKEN, "MP_SELLER_ID": SELLER}), patch.object(payments, "_request_json", return_value={"id": "ORD123", "user_id": SELLER, "checkout_url": "https://www.mercadopago.com.ar/checkout"}) as request:
            payments.create_checkout(self.order(), "https://store.example")
            self.assertIn("online", request.call_args.kwargs["payload"]["config"])

    def test_remote_order_creation_uses_stable_per_order_idempotency_key(self):
        with patch.dict(os.environ, {"BYSG_PAYMENT_MODE": "mercado_pago", "MP_ACCESS_TOKEN": TOKEN, "MP_SELLER_ID": SELLER}), patch.object(payments, "_request_json", return_value={"id": "ORD123", "user_id": SELLER, "checkout_url": "https://www.mercadopago.com.ar/checkout"}) as request:
            order = self.order()
            payments.create_checkout(order, "https://store.example")
            first = request.call_args.kwargs["extra_headers"]["X-Idempotency-Key"]
            payments.create_checkout(order, "https://store.example")
            self.assertEqual(request.call_args.kwargs["extra_headers"]["X-Idempotency-Key"], first)
            payments.create_checkout({**order, "public_token": "another-local-order"}, "https://store.example")
            self.assertNotEqual(request.call_args.kwargs["extra_headers"]["X-Idempotency-Key"], first)

    def test_provider_errors_never_echo_sensitive_text(self):
        errors = [urllib.error.HTTPError("https://api.mercadopago.com/v1/orders", 401, TOKEN, {}, io.BytesIO(TOKEN.encode())), urllib.error.URLError(TOKEN)]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(payments.urllib.request, "urlopen", side_effect=error), self.assertRaises(payments.PaymentProviderError) as caught:
                payments._request_json("/v1/orders", "GET", TOKEN)
            self.assertNotIn(TOKEN, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
