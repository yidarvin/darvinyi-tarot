import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from app import create_app
from crypto import decrypt_api_key, encrypt_api_key
from extensions import db
from models import User


class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "SECRET_KEY": "test-secret-key",
            "DATABASE_URL": "sqlite:///:memory:",
            "FERNET_KEY": Fernet.generate_key().decode(),
        }
        self.env_patcher = patch.dict(os.environ, self.env, clear=False)
        self.env_patcher.start()

        self.app = create_app()
        self.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = self.app.test_client()

        with self.app.app_context():
            db.drop_all()
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        self.env_patcher.stop()

    def test_login_page_loads(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Log in", response.data)

    def test_root_redirects_to_login_when_logged_out(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_dashboard_requires_auth(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_register_then_login_honors_next_redirect(self):
        register_response = self.client.post(
            "/register",
            data={
                "email": "smoke@example.com",
                "username": "smokeuser",
                "password": "password123",
                "confirm_password": "password123",
                "anthropic_api_key": "sk-ant-test-key",
            },
            follow_redirects=False,
        )
        self.assertEqual(register_response.status_code, 302)
        self.assertIn("/onboarding", register_response.headers.get("Location", ""))

        # End session so we can test login flow explicitly.
        logout_response = self.client.post(
            "/logout",
            data={},
            follow_redirects=False,
        )
        self.assertEqual(logout_response.status_code, 302)
        self.assertIn("/login", logout_response.headers.get("Location", ""))

        login_response = self.client.post(
            "/login?next=/history",
            data={
                "identifier": "smoke@example.com",
                "password": "password123",
                "next_url": "/history",
            },
            follow_redirects=False,
        )
        self.assertEqual(login_response.status_code, 302)
        self.assertIn("/history", login_response.headers.get("Location", ""))

        with self.app.app_context():
            self.assertIsNotNone(User.query.filter_by(email="smoke@example.com").first())


class CryptoSmokeTests(unittest.TestCase):
    def test_encrypt_decrypt_round_trip(self):
        with patch.dict(
            os.environ,
            {"FERNET_KEY": Fernet.generate_key().decode()},
            clear=False,
        ):
            encrypted = encrypt_api_key("sk-ant-test-key")
            decrypted = decrypt_api_key(encrypted)
            self.assertEqual(decrypted, "sk-ant-test-key")


if __name__ == "__main__":
    unittest.main()
