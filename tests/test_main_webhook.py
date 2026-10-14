from __future__ import annotations

import os
import sys
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class CapturedWebhook:
    headers = {"x-request-id": "delivery-123", "x-signature": "signed-for-test"}

    def __init__(self):
        self.body = {"type": "order", "data": {"id": "ORDTST123"}}
        self.status = None
        self.response = None

    def _read_json(self):
        return self.body

    def _json(self, status, payload):
        self.status = status
        self.response = payload


class MainWebhookHandlerTests(unittest.TestCase):
    def test_passes_request_id_to_processor_when_body_has_no_event_id(self):
        handler = CapturedWebhook()
        parsed = urlparse("/api/webhooks/mercadopago?data.id=ORDTST123&type=order")
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "false", "MP_SELLER_ID": ""}), \
             patch.object(server.webhooks, "OrderNotifications") as notifications_class:
            notifications = notifications_class.return_value
            notifications.authenticate.return_value = True
            notifications.process.return_value = {"status": "processed"}
            server.BYSGHandler._handle_mercado_pago_webhook(handler, parsed)

        notifications.authenticate.assert_called_once_with(
            "signed-for-test", "delivery-123", "ORDTST123"
        )
        self.assertIsNone(notifications_class.call_args.kwargs["expected_seller_id"])
        notifications.process.assert_called_once_with(
            "ORDTST123", handler.body, "delivery-123"
        )
        self.assertEqual(handler.status, HTTPStatus.OK)
        self.assertEqual(handler.response, {"status": "processed"})

    def test_production_webhook_requires_and_passes_expected_seller(self):
        parsed = urlparse("/api/webhooks/mercadopago?data.id=ORD123&type=order")
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "true", "MP_SELLER_ID": ""}), \
             patch.object(server.webhooks, "OrderNotifications") as notifications_class:
            handler = CapturedWebhook()
            server.BYSGHandler._handle_mercado_pago_webhook(handler, parsed)
            self.assertEqual(handler.status, HTTPStatus.SERVICE_UNAVAILABLE)
            notifications_class.assert_not_called()
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "true", "MP_SELLER_ID": "12345"}), \
             patch.object(server.webhooks, "OrderNotifications") as notifications_class:
            notifications = notifications_class.return_value
            notifications.authenticate.return_value = True
            notifications.process.return_value = {"status": "processed"}
            handler = CapturedWebhook()
            server.BYSGHandler._handle_mercado_pago_webhook(handler, parsed)
            self.assertEqual(handler.status, HTTPStatus.OK)
            self.assertEqual(notifications_class.call_args.kwargs["expected_seller_id"], "12345")


if __name__ == "__main__":
    unittest.main()
