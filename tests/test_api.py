import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from notify_service.api import create_app
from notify_service.config import Settings
from notify_service.policy import normalize_origin
from notify_service.store import Store


class ApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(
            api_key="unit-test-key-not-for-production-12345",
            allowed_origins=frozenset({"https://supplier.example"}),
            db_path=Path(directory.name) / "queue.sqlite3",
        )
        self.client = self.enterContext(TestClient(create_app(self.settings)))
        self.headers = {"X-API-Key": self.settings.api_key, "Idempotency-Key": "event-1"}
        self.payload = {
            "url": "https://supplier.example/hook?token=private-query",
            "method": "POST",
            "headers": {"Authorization": "Bearer private-token", "Content-Type": "application/json"},
            "body": '{"user_id":1}',
        }

    def submit(self, payload=None, headers=None):
        return self.client.post(
            "/v1/notifications",
            json=self.payload if payload is None else payload,
            headers=self.headers if headers is None else headers,
        )

    def test_accept_query_deduplicate_and_hide_secrets(self):
        result = self.submit()
        self.assertEqual(result.status_code, 202)
        notification_id = result.json()["id"]
        self.assertFalse(result.json()["deduplicated"])
        again = self.submit()
        self.assertEqual(again.json()["id"], notification_id)
        self.assertTrue(again.json()["deduplicated"])
        detail = self.client.get(f"/v1/notifications/{notification_id}", headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "pending")
        self.assertEqual(detail.json()["max_attempts"], 6)
        for secret in ["private-token", "private-query", "user_id"]:
            self.assertNotIn(secret, detail.text)
        listing = self.client.get("/v1/notifications?status=pending&limit=1", headers=self.headers)
        self.assertEqual(len(listing.json()), 1)

    def test_authentication_and_required_idempotency(self):
        self.assertEqual(self.submit(headers={}).status_code, 401)
        self.assertEqual(self.submit(headers={"X-API-Key": "wrong"}).status_code, 401)
        self.assertEqual(self.submit(headers={"X-API-Key": self.settings.api_key}).status_code, 422)
        self.assertEqual(self.client.get("/v1/notifications").status_code, 401)
        self.assertEqual(self.client.get("/v1/notifications/missing").status_code, 401)
        self.assertEqual(self.client.post("/v1/notifications/missing/redrive").status_code, 401)
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})

    def test_conflicting_key_returns_409(self):
        self.submit()
        changed = {**self.payload, "body": "changed"}
        self.assertEqual(self.submit(changed).status_code, 409)

    def test_equivalent_header_casing_is_idempotent(self):
        first = self.submit()
        changed = {**self.payload, "headers": {
            name.lower(): value for name, value in self.payload["headers"].items()
        }}
        second = self.submit(changed)
        self.assertEqual(second.json()["id"], first.json()["id"])
        self.assertTrue(second.json()["deduplicated"])

    def test_destination_policy_rejects_untrusted_and_malformed_urls(self):
        for url in [
            "https://evil.example/hook", "http://supplier.example/hook",
            "https://supplier.example:444/hook", "file:///etc/passwd",
            "https://supplier.example.evil.example/hook",
            "https://supplier.example@evil.example/hook",
            "https://user:password@supplier.example/hook",
            "https://supplier.example/hook#fragment",
            "https://supplier.example\\@evil.example/hook",
            "https://supplier.example:0/hook",
            "https://supplier.example:99999/hook",
            "https://[broken/hook", " https://supplier.example/hook",
        ]:
            with self.subTest(url=url):
                self.assertEqual(self.submit({**self.payload, "url": url}).status_code, 422)

    def test_default_port_normalization(self):
        self.assertEqual(normalize_origin("https://supplier.example:443"), "https://supplier.example")
        self.assertEqual(normalize_origin("http://[::1]:9000"), "http://[::1]:9000")
        response = self.submit({**self.payload, "url": "https://supplier.example:443/hook"})
        self.assertEqual(response.status_code, 202)

    def test_invalid_headers_and_body_are_rejected_without_echo(self):
        for headers in [
            {"Host": "evil.example"}, {"X-Notification-Id": "forged"},
            {"X-Secret": "secret\r\ninjected: value"},
            {"Content-Length": "123"}, {"X-Test": "one", "x-test": "two"},
            {"Bad Name": "secret"}, {"X-Big": "a" * 8193},
        ]:
            with self.subTest(headers=list(headers)):
                result = self.submit({**self.payload, "headers": headers})
                self.assertEqual(result.status_code, 422)
                self.assertNotIn("secret", result.text)
                self.assertNotIn('"input"', result.text)
        self.assertEqual(self.submit({**self.payload, "body": "a" * 65537}).status_code, 422)
        self.assertEqual(self.submit({**self.payload, "body": "\u4e2d" * 21846}).status_code, 422)
        self.assertEqual(self.submit({**self.payload, "body": {"not": "a string"}}).status_code, 422)

    def test_raw_request_size_limit(self):
        result = self.client.post(
            "/v1/notifications", content=b" " * 524289,
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertEqual(result.status_code, 413)

    def test_unsendable_header_whitespace_is_rejected_before_acceptance(self):
        for value in [" leading", "trailing ", " "]:
            with self.subTest(value=value):
                response = self.submit({**self.payload, "headers": {"X-Custom": value}})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(Store(self.settings.db_path).list(None, 100), [])

    def test_size_limit_without_content_length(self):
        result = self.client.post(
            "/v1/notifications", content=iter([b" " * 262144, b" " * 262145]),
            headers={**self.headers, "Content-Type": "application/json"},
        )
        self.assertNotIn("content-length", result.request.headers)
        self.assertEqual(result.status_code, 413)

    def test_invalid_method_extra_fields_and_list_limits(self):
        for change in [{"method": "CONNECT"}, {"unexpected": "field"}]:
            self.assertEqual(self.submit({**self.payload, **change}).status_code, 422)
        for query in ["limit=0", "limit=101", "status=invalid"]:
            self.assertEqual(
                self.client.get(f"/v1/notifications?{query}", headers=self.headers).status_code, 422,
            )

    def test_redrive_only_dead_and_retain_identity(self):
        notification_id = self.submit().json()["id"]
        path = f"/v1/notifications/{notification_id}/redrive"
        self.assertEqual(self.client.post(path, headers=self.headers).status_code, 409)
        store = Store(self.settings.db_path)
        claim = store.claim(self.settings.lease_seconds)
        store.finish(claim, success=False, retryable=False, http_status=400, error="http_400")
        result = self.client.post(path, headers=self.headers)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.json()["id"], notification_id)
        self.assertEqual(result.json()["status"], "pending")
        self.assertEqual(result.json()["attempts"], 0)
        self.assertEqual(result.json()["total_attempts"], 1)
        self.assertEqual(self.client.post(path, headers=self.headers).status_code, 409)
        self.assertEqual(self.client.post(
            "/v1/notifications/missing/redrive", headers=self.headers,
        ).status_code, 404)

    def test_database_failure_never_returns_accepted(self):
        with patch.object(Store, "enqueue", side_effect=sqlite3.OperationalError("disk unavailable")):
            result = self.submit()
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.headers["Retry-After"], "1")
        self.assertNotIn("disk unavailable", result.text)

    def test_settings_fail_closed(self):
        for change in [
            {"api_key": ""}, {"api_key": "a" * 32 + "\n"}, {"allowed_origins": frozenset()},
            {"allowed_origins": frozenset({"https://supplier.example/path"})},
            {"max_attempts": 0}, {"max_attempts": 101},
            {"lease_seconds": 10}, {"poll_seconds": float("nan")},
            {"retry_cap_seconds": 1},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(self.settings, **change)
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ValueError):
            Settings.from_env()
