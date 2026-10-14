from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


class CapturedGet:
    _serve_static = server.BYSGHandler._serve_static

    def __init__(self, path: str):
        self.path = path
        self.status = None
        self.payload = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def _json(self, status, payload):
        self.status = status
        self.payload = payload

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass

    def _server_error(self, error):
        raise error


class PublicStaticAccessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        for name in ("index.html", "admin.html", "pago-prueba.html"):
            (self.root / name).write_text(name, encoding="utf-8")
        for directory, name in (
            ("assets/branding", "logo.png"),
            ("assets/catalogo", "draft.jpg"),
            ("assets/referencias", "private.jpeg"),
            ("backups/20261003", "bysg.sqlite3"),
            ("outputs/finanzas", "Finanzas-BYSG.xlsx"),
            ("data", "bysg.sqlite3"),
            ("app", "database.py"),
        ):
            path = self.root / directory
            path.mkdir(parents=True, exist_ok=True)
            (path / name).write_bytes(b"private-or-public-test-content")
        (self.root / ".env").write_text("test-secret", encoding="utf-8")
        (self.root / "assets/branding/.hidden.png").write_bytes(b"test-secret")
        (self.root / "assets/branding/finance.png").symlink_to(
            self.root / "outputs/finanzas/Finanzas-BYSG.xlsx"
        )

    def get(self, path):
        capture = CapturedGet(path)
        with patch.object(server, "ROOT", self.root):
            server.BYSGHandler.do_GET(capture)
        return capture

    def test_public_pages_and_brand_asset_are_served(self):
        for path in ("/", "/muestra-web/index.html", "/admin.html", "/assets/branding/logo.png"):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, HTTPStatus.OK)
                self.assertTrue(response.wfile.getvalue())

    def test_private_files_cannot_be_downloaded_without_authentication(self):
        for path in (
            "/backups/20261003/bysg.sqlite3",
            "/muestra-web/backups/20261003/bysg.sqlite3",
            "/outputs/finanzas/Finanzas-BYSG.xlsx",
            "/data/bysg.sqlite3",
            "/app/database.py",
            "/.env",
            "/assets/referencias/private.jpeg",
            "/assets/catalogo/draft.jpg",
            "/assets/branding/.hidden.png",
            "/assets/branding/finance.png",
            "/%2e%2e/index.html",
        ):
            with self.subTest(path=path):
                response = self.get(path)
                self.assertEqual(response.status, HTTPStatus.NOT_FOUND)
                self.assertEqual(response.wfile.getvalue(), b"")

    def test_mock_payment_page_is_available_only_outside_production(self):
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "false"}):
            self.assertEqual(self.get("/pago-prueba.html").status, HTTPStatus.OK)
        with patch.dict(os.environ, {"BYSG_PRODUCTION": "true"}):
            for path in ("/pago-prueba.html", "/muestra-web/pago-prueba.html"):
                with self.subTest(path=path):
                    self.assertEqual(self.get(path).status, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
