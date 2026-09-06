import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from notify_service.config import Settings
from notify_service.models import NotificationRequest
from notify_service.store import Store
from notify_service.worker import deliver, retry_delay, run_once


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(
            api_key="unit-test-key-not-for-production-12345",
            allowed_origins=frozenset({"https://supplier.example"}),
            db_path=Path(directory.name) / "queue.sqlite3",
        )
        self.store = Store(self.settings.db_path)
        self.store.initialize()
        self.request = NotificationRequest(
            url="https://supplier.example/hook?auth=private",
            method="PATCH", headers={"Content-Type": "application/json", "X-Custom": "abc"},
            body='{"contact_id":42}',
        )

    def enqueue(self, key="event-1", max_attempts=3, now=None):
        return self.store.enqueue(key, self.request, max_attempts, now=now)[0]

    async def test_raw_body_headers_and_success(self):
        notification_id = self.enqueue()
        captured = []

        def handler(request):
            captured.append(request)
            return httpx.Response(204)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertTrue(await run_once(self.store, client, self.settings))
            self.assertFalse(await run_once(self.store, client, self.settings))
        request = captured[0]
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(request.content, b'{"contact_id":42}')
        self.assertEqual(request.headers["content-type"], "application/json")
        self.assertEqual(request.headers["x-custom"], "abc")
        self.assertEqual(request.headers["x-notification-id"], notification_id)
        self.assertEqual(self.store.get(notification_id)["status"], "succeeded")

    async def test_supplier_specific_text_formats_are_preserved(self):
        for index, (content_type, body) in enumerate([
            ("application/json", '{"name":"\\u4e2d\\u6587"}'),
            ("application/xml", "<contact><status>paid</status></contact>"),
            ("application/x-www-form-urlencoded", "sku=A%2B1&quantity=2"),
            ("text/plain; charset=utf-8", "\u4e2d\u6587"),
        ]):
            with self.subTest(content_type=content_type):
                payload = NotificationRequest(
                    url="https://supplier.example/hook", method="POST",
                    headers={"Content-Type": content_type}, body=body,
                )
                notification_id = self.store.enqueue(str(index), payload, 6)[0]
                captured = []

                def handler(request):
                    captured.append(request)
                    return httpx.Response(204)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    await run_once(self.store, client, self.settings)
                self.assertEqual(captured[0].content, body.encode("utf-8"))
                self.assertEqual(captured[0].headers["Content-Type"], content_type)
                self.assertEqual(self.store.get(notification_id)["status"], "succeeded")

    async def test_response_classification_and_no_redirects(self):
        for code in [200, 201, 202, 204, 299, 301, 302, 307, 400, 401, 403, 404, 408, 425, 429, 500, 503]:
            with self.subTest(code=code):
                notification_id = self.enqueue(str(code))
                captured = []

                def handler(request):
                    captured.append(request)
                    return httpx.Response(code, headers={
                        "Location": "https://untrusted.example/", "Retry-After": "30",
                    })

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), follow_redirects=True,
                ) as client:
                    await run_once(self.store, client, self.settings)
                self.assertEqual(len(captured), 1)
                expected = "succeeded" if code < 300 else (
                    "pending" if code in {408, 425, 429, 500, 503} else "dead"
                )
                row = self.store.get(notification_id)
                self.assertEqual(row["status"], expected)
                if expected == "pending":
                    self.assertGreaterEqual(row["next_attempt_at"] - row["updated_at"], 30)

    async def test_transport_failure_is_retried(self):
        for error_type in [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError]:
            with self.subTest(error=error_type.__name__):
                notification_id = self.enqueue(error_type.__name__)

                def handler(request):
                    raise error_type("private endpoint details", request=request)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    await run_once(self.store, client, self.settings)
                row = self.store.get(notification_id)
                self.assertEqual(row["status"], "pending")
                self.assertEqual(row["last_error"], error_type.__name__)
                self.assertNotIn("private", str(row))

    async def test_whole_attempt_deadline(self):
        self.enqueue()
        claim = self.store.claim(30)

        async def handler(request):
            await asyncio.sleep(1)
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await deliver(
                claim, client, replace(self.settings, attempt_timeout_seconds=0.02),
            )
        self.assertEqual(result.error, "attempt_deadline_exceeded")
        self.assertTrue(result.retryable)

    async def test_no_response_body_consumption(self):
        class UnreadableBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise AssertionError("Response body must not be consumed")
                yield b""  # pragma: no cover

        notification_id = self.enqueue()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=UnreadableBody()),
        )) as client:
            await run_once(self.store, client, self.settings)
        self.assertEqual(self.store.get(notification_id)["status"], "succeeded")

    async def test_destination_is_rechecked_at_delivery(self):
        notification_id = self.enqueue()
        settings = replace(self.settings, allowed_origins=frozenset({"https://new.example"}))

        def handler(request):
            self.fail("Removed origin must not receive a request")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_once(self.store, client, settings)
        row = self.store.get(notification_id)
        self.assertEqual(row["status"], "dead")
        self.assertEqual(row["last_error"], "destination_not_allowed")

    async def test_cookies_are_not_shared_between_notifications(self):
        self.enqueue("one")
        self.enqueue("two")
        cookies = []

        def handler(request):
            cookies.append(request.headers.get("cookie"))
            return httpx.Response(200, headers={"Set-Cookie": "session=private; Path=/"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await run_once(self.store, client, self.settings)
            await run_once(self.store, client, self.settings)
        self.assertEqual(cookies, [None, None])

    async def test_crash_after_delivery_repeats_with_stable_id(self):
        notification_id = self.enqueue(now=100)
        first = self.store.claim(30, now=100)
        received = []

        def handler(request):
            received.append(request.headers["X-Notification-Id"])
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            self.assertTrue((await deliver(first, client, self.settings)).success)
            # Simulate termination after the supplier accepted but before the local commit.
            restarted_store = Store(self.settings.db_path)
            second = restarted_store.claim(30, now=130)
            self.assertTrue((await deliver(second, client, self.settings)).success)
            restarted_store.finish(
                second, success=True, retryable=False, http_status=200, error=None, now=131,
            )
        self.assertEqual(received, [notification_id, notification_id])
        self.assertEqual(self.store.get(notification_id)["status"], "succeeded")

    def test_exponential_jitter_and_retry_after(self):
        now = 1700000000
        with patch("notify_service.worker.random.uniform", side_effect=lambda low, high: high):
            self.assertEqual(retry_delay(1, None, self.settings, now), 30)
            self.assertEqual(retry_delay(3, None, self.settings, now), 120)
            self.assertEqual(retry_delay(100, None, self.settings, now), 3600)
            self.assertEqual(retry_delay(1, "60", self.settings, now), 60)
            self.assertEqual(retry_delay(1, "9" * 5000, self.settings, now), 3600)
            self.assertEqual(retry_delay(1, "invalid", self.settings, now), 30)
            self.assertEqual(retry_delay(1, "-10", self.settings, now), 30)
            date = format_datetime(datetime.fromtimestamp(now + 120, timezone.utc), usegmt=True)
            self.assertEqual(retry_delay(1, date, self.settings, now), 120)
            past = format_datetime(datetime.fromtimestamp(now - 120, timezone.utc), usegmt=True)
            self.assertEqual(retry_delay(1, past, self.settings, now), 30)

    def test_retry_after_leading_zeros_do_not_turn_seconds_into_hours(self):
        with patch("notify_service.worker.random.uniform", side_effect=lambda low, high: low):
            self.assertEqual(retry_delay(1, "00000000001", self.settings, 1700000000), 15)

    def test_default_retry_wait_budget_is_465_to_930_seconds(self):
        for select, expected in [(lambda low, high: low, 465), (lambda low, high: high, 930)]:
            with patch("notify_service.worker.random.uniform", side_effect=select):
                total = sum(
                    retry_delay(attempt, None, self.settings, 1700000000)
                    for attempt in range(1, self.settings.max_attempts)
                )
            self.assertEqual(total, expected)
