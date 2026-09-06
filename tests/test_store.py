import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from notify_service.models import NotificationRequest
from notify_service.store import ConflictError, NotFoundError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "queue.sqlite3")
        self.store.initialize()
        self.request = NotificationRequest(url="https://supplier.example/hook", body='{"id":1}')

    def enqueue(self, key="event-1", attempts=3):
        return self.store.enqueue(key, self.request, attempts, now=100)[0]

    def test_commit_survives_new_store_instance(self):
        notification_id = self.enqueue()
        reopened = Store(self.store.path)
        reopened.initialize()
        self.assertEqual(reopened.get(notification_id)["status"], "pending")
        with reopened.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)

    def test_idempotent_acceptance_and_conflict(self):
        notification_id = self.enqueue()
        self.assertEqual(
            self.store.enqueue("event-1", self.request, 3, now=101),
            (notification_id, "pending", True),
        )
        changed = self.request.model_copy(update={"body": "changed"})
        with self.assertRaises(ConflictError):
            self.store.enqueue("event-1", changed, 3, now=101)
        self.assertEqual(len(self.store.list(None, 100)), 1)

    def test_concurrent_idempotency_creates_one_job(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(
                lambda _: self.store.enqueue("same-key", self.request, 3, now=100),
                range(16),
            ))
        self.assertEqual(len({item[0] for item in results}), 1)
        self.assertEqual(sum(not item[2] for item in results), 1)

    def test_concurrent_workers_claim_each_job_once(self):
        ids = {self.enqueue(str(index)) for index in range(16)}
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims = list(executor.map(
                lambda _: Store(self.store.path).claim(30, now=100), range(24),
            ))
        claimed_ids = [claim.id for claim in claims if claim is not None]
        self.assertEqual(set(claimed_ids), ids)
        self.assertEqual(len(claimed_ids), len(ids))

    def test_retry_is_not_claimed_early(self):
        notification_id = self.enqueue()
        claim = self.store.claim(30, now=100)
        self.assertIsNotNone(claim)
        self.assertTrue(self.store.finish(
            claim, success=False, retryable=True, http_status=503,
            error="http_503", retry_delay=10, now=101,
        ))
        self.assertIsNone(self.store.claim(30, now=110))
        second = self.store.claim(30, now=111)
        self.assertEqual(second.attempt, 2)
        self.store.finish(
            second, success=True, retryable=False, http_status=204, error=None, now=112,
        )
        row = self.store.get(notification_id)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["total_attempts"], 2)
        self.assertEqual([item["outcome"] for item in row["history"]],
                         ["retryable_failure", "succeeded"])
        self.assertIsNone(self.store.claim(30, now=999))

    def test_retry_exhaustion_and_redrive_preserve_history(self):
        notification_id = self.enqueue(attempts=1)
        claim = self.store.claim(30, now=100)
        self.store.finish(
            claim, success=False, retryable=True, http_status=503, error="http_503", now=101,
        )
        self.assertEqual(self.store.get(notification_id)["status"], "dead")
        self.store.redrive(notification_id, now=102)
        second = self.store.claim(30, now=102)
        self.assertEqual(second.id, claim.id)
        self.assertEqual(second.attempt, 1)
        self.store.finish(
            second, success=True, retryable=False, http_status=200, error=None, now=103,
        )
        row = self.store.get(notification_id)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["total_attempts"], 2)
        self.assertEqual([item["attempt_no"] for item in row["history"]], [1, 2])

    def test_exactly_six_attempts_before_dead(self):
        notification_id = self.enqueue(attempts=6)
        for attempt in range(1, 7):
            now = 100 + attempt * 2
            claim = self.store.claim(30, now=now)
            self.assertEqual(claim.attempt, attempt)
            self.store.finish(
                claim, success=False, retryable=True, http_status=503, error="http_503", now=now + 1,
            )
            expected = "pending" if attempt < 6 else "dead"
            self.assertEqual(self.store.get(notification_id)["status"], expected)
        self.assertIsNone(self.store.claim(30, now=999))
        self.assertEqual(len(self.store.get(notification_id)["history"]), 6)

    def test_permanent_failure_and_redrive_guards(self):
        notification_id = self.enqueue()
        with self.assertRaises(ConflictError):
            self.store.redrive(notification_id)
        with self.assertRaises(NotFoundError):
            self.store.redrive("missing")
        claim = self.store.claim(30, now=100)
        self.store.finish(
            claim, success=False, retryable=False, http_status=400, error="http_400", now=101,
        )
        self.assertEqual(self.store.get(notification_id)["status"], "dead")

    def test_crash_recovery_and_stale_token_fencing(self):
        notification_id = self.enqueue()
        old = self.store.claim(30, now=100)
        self.assertIsNone(self.store.claim(30, now=129))
        new = Store(self.store.path).claim(30, now=130)
        self.assertEqual(new.id, old.id)
        self.assertNotEqual(new.token, old.token)
        self.assertFalse(self.store.finish(
            old, success=True, retryable=False, http_status=200, error=None, now=131,
        ))
        self.assertTrue(self.store.finish(
            new, success=True, retryable=False, http_status=200, error=None, now=132,
        ))
        history = self.store.get(notification_id)["history"]
        self.assertEqual(history[0]["outcome"], "unknown")
        self.assertEqual(history[0]["error"], "lease_expired")
        self.assertEqual(history[1]["outcome"], "succeeded")

    def test_expired_lease_cannot_finish_without_replacement(self):
        self.enqueue()
        claim = self.store.claim(30, now=100)
        self.assertFalse(self.store.finish(
            claim, success=True, retryable=False, http_status=200, error=None, now=130,
        ))

    def test_claim_lease_starts_after_acquiring_write_lock(self):
        notification_id = self.enqueue()
        connect = self.store.connect
        with patch("notify_service.store.time.time", return_value=100) as clock:
            @contextmanager
            def delayed_connect(*, write=False):
                with connect(write=write) as connection:
                    clock.return_value = 104
                    yield connection

            with patch.object(self.store, "connect", side_effect=delayed_connect):
                claim = self.store.claim(30)
        self.assertEqual(claim.id, notification_id)
        self.assertEqual(self.store.get(notification_id)["lease_until"], 134)

    def test_lease_expiring_during_lock_wait_cannot_finish(self):
        notification_id = self.enqueue()
        claim = self.store.claim(30, now=100)
        connect = self.store.connect
        with patch("notify_service.store.time.time", return_value=129) as clock:
            @contextmanager
            def delayed_connect(*, write=False):
                with connect(write=write) as connection:
                    clock.return_value = 131
                    yield connection

            with patch.object(self.store, "connect", side_effect=delayed_connect):
                saved = self.store.finish(
                    claim, success=True, retryable=False, http_status=200, error=None,
                )
        self.assertFalse(saved)
        self.assertEqual(self.store.get(notification_id)["status"], "in_flight")

    def test_last_attempt_crash_becomes_dead(self):
        notification_id = self.enqueue(attempts=1)
        self.store.claim(30, now=100)
        self.assertIsNone(self.store.claim(30, now=130))
        row = self.store.get(notification_id)
        self.assertEqual(row["status"], "dead")
        self.assertEqual(row["history"][0]["outcome"], "unknown")

    def test_invalid_stored_request_is_quarantined_without_blocking_queue(self):
        broken_id = self.enqueue("old-invalid-event")
        with self.store.connect(write=True) as connection:
            connection.execute(
                "UPDATE notifications SET payload=? WHERE id=?",
                ('{"url":"https://supplier.example/hook","headers":{"X-Test":" invalid"}}', broken_id),
            )
        next_id = self.store.enqueue("next-event", self.request, 6, now=101)[0]
        claim = self.store.claim(30, now=102)
        self.assertEqual(claim.id, next_id)
        broken = self.store.get(broken_id)
        self.assertEqual(broken["status"], "dead")
        self.assertEqual(broken["last_error"], "invalid_stored_request")
        self.assertEqual(broken["history"][0]["outcome"], "permanent_failure")

    def test_exception_rolls_back_transaction(self):
        notification_id = self.enqueue()
        with self.assertRaises(RuntimeError):
            with self.store.connect(write=True) as connection:
                connection.execute("DELETE FROM notifications WHERE id=?", (notification_id,))
                raise RuntimeError("simulated failure")
        self.assertEqual(self.store.get(notification_id)["status"], "pending")

    def test_list_filter_and_missing_detail(self):
        self.enqueue()
        self.assertEqual(len(self.store.list("pending", 1)), 1)
        self.assertEqual(self.store.list("dead", 1), [])
        with self.assertRaises(NotFoundError):
            self.store.get("missing")
