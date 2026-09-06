import hashlib
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from .models import NotificationRequest, Status


logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'in_flight', 'succeeded', 'dead')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    total_attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL,
    next_attempt_at REAL,
    lease_until REAL,
    claim_token TEXT,
    last_http_status INTEGER,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS notifications_due ON notifications(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS notifications_leases ON notifications(status, lease_until);
CREATE INDEX IF NOT EXISTS notifications_created ON notifications(created_at, id);
CREATE TABLE IF NOT EXISTS attempts (
    notification_id TEXT NOT NULL REFERENCES notifications(id),
    attempt_no INTEGER NOT NULL,
    cycle_attempt_no INTEGER NOT NULL,
    claim_token TEXT NOT NULL UNIQUE,
    started_at REAL NOT NULL,
    finished_at REAL,
    outcome TEXT NOT NULL,
    http_status INTEGER,
    error TEXT,
    PRIMARY KEY (notification_id, attempt_no)
);
"""

PUBLIC_COLUMNS = (
    "id, status, created_at, updated_at, attempts, total_attempts, max_attempts, "
    "next_attempt_at, lease_until, last_http_status, last_error"
)


class ConflictError(Exception):
    pass


class NotFoundError(Exception):
    pass


@dataclass(frozen=True)
class Claim:
    id: str
    token: str
    attempt: int
    max_attempts: int
    request: NotificationRequest


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        finally:
            # Closing an uncommitted transaction rolls it back, including on errors.
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)

    def health(self) -> None:
        with self.connect() as connection:
            connection.execute("SELECT id FROM notifications LIMIT 1").fetchone()

    def enqueue(
        self, key: str, request: NotificationRequest, max_attempts: int, now: float | None = None,
    ) -> tuple[str, Status, bool]:
        now = time.time() if now is None else now
        payload = json.dumps(request.model_dump(), sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect(write=True) as connection:
            existing = connection.execute(
                "SELECT id, fingerprint, status FROM notifications WHERE idempotency_key=?", (key,),
            ).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise ConflictError("Idempotency-Key already exists with different content")
                return existing["id"], existing["status"], True
            notification_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO notifications
                   (id, idempotency_key, fingerprint, payload, status, created_at, updated_at,
                    max_attempts, next_attempt_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (notification_id, key, fingerprint, payload, now, now, max_attempts, now),
            )
        return notification_id, "pending", False

    def get(self, notification_id: str) -> dict:
        with self.connect() as connection:
            # Keep the status and its attempt history in one read snapshot.
            connection.execute("BEGIN")
            row = connection.execute(
                f"SELECT {PUBLIC_COLUMNS} FROM notifications WHERE id=?", (notification_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("Notification not found")
            history = connection.execute(
                """SELECT attempt_no, cycle_attempt_no, started_at, finished_at, outcome,
                          http_status, error FROM attempts
                   WHERE notification_id=? ORDER BY attempt_no DESC LIMIT 100""",
                (notification_id,),
            ).fetchall()
            return {**dict(row), "history": [dict(item) for item in reversed(history)]}

    def list(self, status: Status | None, limit: int) -> list[dict]:
        with self.connect() as connection:
            if status is None:
                rows = connection.execute(
                    f"SELECT {PUBLIC_COLUMNS} FROM notifications ORDER BY created_at DESC, id LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"""SELECT {PUBLIC_COLUMNS} FROM notifications WHERE status=?
                        ORDER BY created_at DESC, id LIMIT ?""", (status, limit),
                ).fetchall()
            return [dict(row) for row in rows]

    def claim(self, lease_seconds: float, now: float | None = None) -> Claim | None:
        requested_time = now
        with self.connect(write=True) as connection:
            while True:
                now = time.time() if requested_time is None else requested_time
                row = connection.execute(
                    """SELECT * FROM notifications
                       WHERE (status='pending' AND next_attempt_at<=?)
                          OR (status='in_flight' AND lease_until<=?)
                       ORDER BY COALESCE(next_attempt_at, lease_until), created_at, id LIMIT 1""",
                    (now, now),
                ).fetchone()
                if row is None:
                    return None
                if row["status"] == "in_flight":
                    connection.execute(
                        """UPDATE attempts SET outcome='unknown', error='lease_expired',
                           finished_at=? WHERE claim_token=? AND outcome='in_flight'""",
                        (now, row["claim_token"]),
                    )
                if row["attempts"] >= row["max_attempts"]:
                    connection.execute(
                        """UPDATE notifications SET status='dead', updated_at=?, next_attempt_at=NULL,
                           lease_until=NULL, claim_token=NULL, last_http_status=NULL,
                           last_error='lease_expired' WHERE id=?""", (now, row["id"]),
                    )
                    continue
                token = str(uuid.uuid4())
                attempt = row["attempts"] + 1
                total = row["total_attempts"] + 1
                connection.execute(
                    """UPDATE notifications SET status='in_flight', updated_at=?, attempts=?,
                       total_attempts=?, next_attempt_at=NULL, lease_until=?, claim_token=?
                       WHERE id=?""",
                    (now, attempt, total, now + lease_seconds, token, row["id"]),
                )
                connection.execute(
                    """INSERT INTO attempts
                       (notification_id, attempt_no, cycle_attempt_no, claim_token, started_at, outcome)
                       VALUES (?, ?, ?, ?, ?, 'in_flight')""",
                    (row["id"], total, attempt, token, now),
                )
                try:
                    request = NotificationRequest.model_validate_json(row["payload"])
                except ValidationError:
                    # Old or corrupt payloads must not block every subsequent notification.
                    connection.execute(
                        """UPDATE notifications SET status='dead', lease_until=NULL, claim_token=NULL,
                           last_http_status=NULL, last_error='invalid_stored_request' WHERE id=?""",
                        (row["id"],),
                    )
                    connection.execute(
                        """UPDATE attempts SET finished_at=?, outcome='permanent_failure',
                           error='invalid_stored_request' WHERE claim_token=?""", (now, token),
                    )
                    logger.warning("invalid_stored_request id=%s", row["id"])
                    continue
                return Claim(row["id"], token, attempt, row["max_attempts"], request)

    def finish(
        self, claim: Claim, *, success: bool, retryable: bool,
        http_status: int | None, error: str | None, retry_delay: float = 0,
        now: float | None = None,
    ) -> bool:
        status = "succeeded" if success else (
            "pending" if retryable and claim.attempt < claim.max_attempts else "dead"
        )
        outcome = "succeeded" if success else (
            "retryable_failure" if retryable else "permanent_failure"
        )
        with self.connect(write=True) as connection:
            # Lock acquisition may wait: fence using the time inside the transaction.
            now = time.time() if now is None else now
            changed = connection.execute(
                """UPDATE notifications SET status=?, updated_at=?, next_attempt_at=?,
                   lease_until=NULL, claim_token=NULL, last_http_status=?, last_error=?
                   WHERE id=? AND status='in_flight' AND claim_token=? AND lease_until>?""",
                (
                    status, now, now + retry_delay if status == "pending" else None,
                    http_status, error, claim.id, claim.token, now,
                ),
            ).rowcount
            if changed != 1:
                return False
            connection.execute(
                """UPDATE attempts SET finished_at=?, outcome=?, http_status=?, error=?
                   WHERE claim_token=?""", (now, outcome, http_status, error, claim.token),
            )
        return True

    def redrive(self, notification_id: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.connect(write=True) as connection:
            row = connection.execute(
                "SELECT status FROM notifications WHERE id=?", (notification_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("Notification not found")
            if row["status"] != "dead":
                raise ConflictError("Only dead notifications can be redriven")
            connection.execute(
                """UPDATE notifications SET status='pending', attempts=0, updated_at=?,
                   next_attempt_at=?, lease_until=NULL, claim_token=NULL,
                   last_http_status=NULL, last_error=NULL WHERE id=?""",
                (now, now, notification_id),
            )
