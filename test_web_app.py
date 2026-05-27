from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from app.main import app


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("EXPORTER_ADMIN_PASSWORD", None)

    def test_healthz(self):
        client = TestClient(app)

        response = client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_index_renders_export_form(self):
        client = TestClient(app)

        response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("IntakeQ Exporter", response.text)
        self.assertIn("All clients", response.text)
        self.assertIn("Start export", response.text)

    def test_admin_password_enables_basic_auth(self):
        os.environ["EXPORTER_ADMIN_PASSWORD"] = "secret"
        client = TestClient(app)

        unauthenticated = client.get("/")
        authenticated = client.get("/", auth=("admin", "secret"))

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(authenticated.status_code, 200)


if __name__ == "__main__":
    unittest.main()

