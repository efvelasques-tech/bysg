from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import database  # noqa: E402
import mailer  # noqa: E402
import payments  # noqa: E402


class BYSGSystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        database.DATABASE_PATH = Path(self.tempdir.name) / "test.sqlite3"
        database.initialize_database(reset=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def payload(self, items):
        return {
            "customer": {"name": "Persona Adulta", "email": "adulto@example.com", "phone": "3515555555"},
            "delivery_method": "pickup",
            "items": items,
        }

    def item(self, product, quantity=1, size=None):
        default_sizes = {"REM": "S", "BUZ": "M", "GOR": "Talle único ajustable"}
        return {"variant_id": product["variant_id"], "size_label": size or default_sizes[product["category_code"]], "quantity": quantity}

    def approve_price(self, category_code):
        row = next(item for item in database.category_financials() if item["code"] == category_code)
        database.update_category_financials({**row, "is_sample_price": False})

    def test_catalog_has_thirty_variants_and_shared_stock(self):
        data = database.catalog()
        self.assertEqual(len(data["products"]), 30)
        self.assertEqual({row["available"] for row in data["stock"]}, {10})
        self.assertTrue(all(product["sku"].startswith("BYSG-") for product in data["products"]))

    def test_existing_database_gains_idempotency_columns_without_reset(self):
        database.DATABASE_PATH.unlink()
        old_schema = database.SCHEMA_PATH.read_text(encoding="utf-8")
        old_schema = old_schema.replace("  client_request_id TEXT,\n", "").replace("  request_hash TEXT,\n", "")
        with database.connect() as connection:
            connection.executescript(old_schema)
        database.initialize_database()
        with database.connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(orders)")}
            indexes = {row["name"] for row in connection.execute("PRAGMA index_list(orders)")}
        self.assertIn("client_request_id", columns)
        self.assertIn("request_hash", columns)
        self.assertIn("idx_orders_client_request_id", indexes)
        self.assertIn("idx_orders_provider_order_id", indexes)

    def test_public_catalog_exposes_no_internal_cost_or_margin_fields(self):
        public_product_fields = {
            "id", "category_code", "category", "design_number", "name", "description",
            "color", "variant_id", "sku", "size_label", "price_cents", "is_sample_price",
        }
        catalog = database.catalog()
        self.assertTrue(catalog["products"])
        for product in catalog["products"]:
            self.assertEqual(set(product), public_product_fields)
        self.assertIn("cost_cents", database.category_financials()[0])
        self.assertIn("margin_cents", database.category_financials()[0])

    def test_approved_payment_moves_reserved_stock_to_sold(self):
        product = database.catalog()["products"][0]
        order = database.create_order(self.payload([self.item(product, 2)]))
        rem = next(row for row in database.stock_status() if row["code"] == product["category_code"])
        self.assertEqual((rem["reserved"], rem["sold"], rem["available"]), (2, 0, 8))
        database.apply_payment_status(order["public_token"], "approved", "MOCK-APPROVED")
        rem = next(row for row in database.stock_status() if row["code"] == product["category_code"])
        self.assertEqual((rem["reserved"], rem["sold"], rem["available"]), (0, 2, 8))
        self.assertGreater(database.dashboard()["bauti_account"]["balance_cents"], 0)

    def test_rejected_payment_releases_capacity(self):
        product = database.catalog()["products"][10]
        order = database.create_order(self.payload([self.item(product, 4)]))
        database.apply_payment_status(order["public_token"], "rejected", "MOCK-REJECTED")
        row = next(item for item in database.stock_status() if item["code"] == product["category_code"])
        self.assertEqual((row["reserved"], row["sold"], row["available"]), (0, 0, 10))

    def test_repeated_browser_request_reuses_one_order_and_one_reservation(self):
        product = database.catalog()["products"][0]
        payload = self.payload([self.item(product)])
        request_id = str(uuid.uuid4())
        first, created = database.create_or_get_order(payload, "mock", request_id)
        repeated, created_again = database.create_or_get_order(payload, "mock", request_id)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["public_token"], repeated["public_token"])
        self.assertEqual(len(database.dashboard()["orders"]), 1)
        self.assertEqual(database.stock_status()[0]["reserved"], 1)
        changed = self.payload([self.item(product, quantity=2)])
        with self.assertRaisesRegex(ValueError, "ya corresponde a otro pedido"):
            database.create_or_get_order(changed, "mock", request_id)
        self.assertEqual(database.stock_status()[0]["reserved"], 1)
        self.assertNotIn("client_request_id", database.get_order(first["public_token"]))
        self.assertNotIn("request_hash", database.get_order(first["public_token"]))

    def test_provider_order_link_cannot_be_replaced_or_reused(self):
        product = database.catalog()["products"][0]
        first = database.create_order(self.payload([self.item(product)]))
        second = database.create_order(self.payload([self.item(product)]))
        database.attach_provider_order(first["public_token"], "ORDTST123")
        database.attach_provider_order(first["public_token"], "ORDTST123")
        with self.assertRaisesRegex(ValueError, "ya está vinculado"):
            database.attach_provider_order(first["public_token"], "ORDTSTOTHER")
        with self.assertRaises(sqlite3.IntegrityError):
            database.attach_provider_order(second["public_token"], "ORDTST123")
        self.assertIsNone(database.get_order(second["public_token"])["provider_order_id"])

    def test_category_capacity_cannot_be_exceeded(self):
        products = [item for item in database.catalog()["products"] if item["category_code"] == "GOR"]
        order = database.create_order(self.payload([self.item(products[0], 10)]))
        self.assertIsNotNone(order["order_number"])
        with self.assertRaises(ValueError):
            database.create_order(self.payload([self.item(products[1], 1)]))

    def test_selected_size_is_saved_and_invalid_size_is_rejected(self):
        remera = next(item for item in database.catalog()["products"] if item["category_code"] == "REM")
        order = database.create_order(self.payload([self.item(remera, size="3XL")]))
        self.assertEqual(order["items"][0]["size_label"], "3XL")
        with self.assertRaises(ValueError):
            database.create_order(self.payload([self.item(remera, size="XS")]))

    def test_delivery_service_is_preserved_for_key_west(self):
        gorra = next(item for item in database.catalog()["products"] if item["category_code"] == "GOR")
        payload = self.payload([self.item(gorra)])
        payload["delivery_method"] = "international_key_west"
        payload["delivery_address"] = "Key West, Florida"
        with patch.dict(os.environ, {"BYSG_SHIPPING_KEY_WEST": "250000"}):
            order = database.create_order(payload)
        self.assertEqual(order["delivery_method"], "mail")
        self.assertEqual(order["delivery_service"], "Envío internacional a Key West")
        self.assertEqual(order["shipping_cents"], 250000)

    def test_real_checkout_rejects_sample_price_before_reserving_stock(self):
        product = database.catalog()["products"][0]
        payload = self.payload([self.item(product)])
        with self.assertRaisesRegex(ValueError, "precio.*muestra"):
            database.create_order(payload, provider="mercado_pago")
        self.assertEqual(database.dashboard()["orders"], [])
        self.assertEqual(database.stock_status()[0]["reserved"], 0)

        self.approve_price(product["category_code"])
        order = database.create_order(payload, provider="mercado_pago")
        self.assertEqual(order["provider"], "mercado_pago")
        self.assertEqual(database.stock_status()[0]["reserved"], 1)

    def test_real_checkout_rejects_approved_zero_price_without_affecting_test_modes(self):
        product = database.catalog()["products"][0]
        row = next(item for item in database.category_financials() if item["code"] == product["category_code"])
        database.update_category_financials({**row, "price_cents": 0, "is_sample_price": False})
        payload = self.payload([self.item(product)])

        with self.assertRaisesRegex(ValueError, "precio.*mayor a cero"):
            database.create_order(payload, provider="mercado_pago")
        self.assertEqual(database.dashboard()["orders"], [])
        self.assertEqual(database.stock_status()[0]["reserved"], 0)

        for provider in ("mock", "mercado_pago_prueba"):
            with self.subTest(provider=provider):
                order = database.create_order(payload, provider=provider)
                self.assertEqual(order["total_cents"], 0)

    def test_every_checkout_requires_defined_shipping_before_reserving_stock(self):
        product = database.catalog()["products"][0]
        self.approve_price(product["category_code"])
        payload = self.payload([self.item(product)])
        payload.update({"delivery_method": "home_delivery", "delivery_address": "Calle de prueba 123"})
        with patch.dict(os.environ, {"BYSG_SHIPPING_HOME": ""}):
            for provider in ("mock", "mercado_pago_prueba", "mercado_pago"):
                with self.subTest(provider=provider):
                    with self.assertRaisesRegex(ValueError, "costo de entrega.*no está definido"):
                        database.create_order(payload, provider=provider)
                    self.assertEqual(database.dashboard()["orders"], [])
                    self.assertEqual(database.stock_status()[0]["reserved"], 0)
        with patch.dict(os.environ, {"BYSG_SHIPPING_HOME": "125000"}):
            order = database.create_order(payload, provider="mercado_pago")
        self.assertEqual(order["shipping_cents"], 125000)

    def test_invalid_or_negative_shipping_rate_is_never_silently_free(self):
        product = database.catalog()["products"][0]
        payload = self.payload([self.item(product)])
        payload.update({"delivery_method": "home_delivery", "delivery_address": "Calle de prueba 123"})
        for rate in ("-1", "abc"):
            with self.subTest(rate=rate), patch.dict(os.environ, {"BYSG_SHIPPING_HOME": rate}):
                self.assertIsNone(database.catalog()["shipping"]["home_delivery"])
                with self.assertRaisesRegex(ValueError, "costo de entrega.*no está definido"):
                    database.create_order(payload)
        self.assertEqual(database.dashboard()["orders"], [])

    def test_webhook_signature_matches_official_manifest(self):
        data_id, request_id, timestamp, secret = "ORD123", "REQ456", "1742505638683", "secret-demo"
        manifest = f"id:{data_id};request-id:{request_id};ts:{timestamp};"
        digest = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
        signature = f"ts={timestamp},v1={digest}"
        self.assertTrue(payments.verify_webhook_signature(signature, request_id, data_id, secret))
        self.assertFalse(payments.verify_webhook_signature(signature, request_id, data_id, "wrong"))

    def test_financials_update_price_cost_margin_and_capacity(self):
        initial = {row["code"]: row for row in database.category_financials()}
        self.assertEqual(initial["REM"]["cost_cents"], 2_770_000)
        updated = database.update_category_financials(
            {
                "code": "REM",
                "finished_product_cents": 2_500_000,
                "bag_cents": 70_000,
                "keychain_cents": 200_000,
                "other_cost_cents": 30_000,
                "price_cents": 4_000_000,
                "capacity": 12,
                "is_sample_price": False,
            }
        )
        self.assertEqual(updated["cost_cents"], 2_800_000)
        self.assertEqual(updated["margin_cents"], 1_200_000)
        self.assertEqual(updated["margin_percent"], 30)
        self.assertEqual(updated["capacity"], 12)
        remeras = [item for item in database.catalog()["products"] if item["category_code"] == "REM"]
        self.assertEqual({item["price_cents"] for item in remeras}, {4_000_000})
        self.assertTrue(all("cost_cents" not in item for item in remeras))
        self.assertEqual({item["is_sample_price"] for item in remeras}, {0})

    def test_cancelling_pending_order_releases_reserved_stock(self):
        product = database.catalog()["products"][0]
        order = database.create_order(self.payload([self.item(product, 3)]))
        cancelled = database.set_order_status(order["public_token"], "cancelled")
        row = next(item for item in database.stock_status() if item["code"] == product["category_code"])
        self.assertEqual((row["reserved"], row["sold"], row["available"]), (0, 0, 10))
        self.assertEqual(cancelled["payment_status"], "rejected")
        self.assertEqual(cancelled["order_status"], "cancelled")

    def test_public_forms_are_saved_and_withdrawal_gets_confirmation(self):
        database.subscribe_newsletter("CLIENTE@EXAMPLE.COM")
        database.subscribe_newsletter("cliente@example.com")
        database.create_contact_message("Cliente", "cliente@example.com", "Quiero consultar por un talle.")
        result = database.create_withdrawal_request("BYSG-000001", "cliente@example.com", "Quiero cancelar.")
        dashboard = database.dashboard()
        self.assertEqual(dashboard["newsletter_subscribers"], 1)
        self.assertEqual(len(dashboard["contact_messages"]), 1)
        self.assertEqual(len(dashboard["withdrawal_requests"]), 1)
        self.assertTrue(result["confirmation_code"].startswith("ARR-"))

    def test_order_emails_are_idempotently_queued(self):
        product = database.catalog()["products"][0]
        order = database.create_order(self.payload([self.item(product, 1)]))
        self.assertTrue(mailer.queue_order_event(order, "payment_pending", "http://127.0.0.1:4180"))
        self.assertFalse(mailer.queue_order_event(order, "payment_pending", "http://127.0.0.1:4180"))
        self.assertEqual(len(database.pending_emails()), 1)


if __name__ == "__main__":
    unittest.main()
