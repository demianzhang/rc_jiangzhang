import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

from .config import Settings
from .policy import normalize_url
from .store import Claim, Store


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    retryable: bool
    http_status: int | None = None
    error: str | None = None
    retry_after: str | None = None


def retry_delay(
    attempt: int, retry_after: str | None, settings: Settings, now: float,
) -> float:
    ceiling = min(settings.retry_cap_seconds, settings.retry_base_seconds * 2 ** (attempt - 1))
    delay = random.uniform(ceiling / 2, ceiling)
    if retry_after is None:
        return delay
    value = retry_after.strip()
    if value.isascii() and value.isdigit():
        # Avoid unbounded integer parsing of an untrusted response header.
        value = value.lstrip("0") or "0"
        suggested = float(value) if len(value) <= 10 else settings.retry_after_cap_seconds
    else:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            suggested = max(0, date.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            logger.warning("invalid_retry_after")
            return delay
    return max(delay, min(suggested, settings.retry_after_cap_seconds))


async def deliver(claim: Claim, client: httpx.AsyncClient, settings: Settings) -> DeliveryResult:
    if normalize_url(claim.request.url)[1] not in settings.allowed_origins:
        return DeliveryResult(False, False, error="destination_not_allowed")
    headers = {**claim.request.headers, "X-Notification-Id": claim.id}
    body = claim.request.body.encode("utf-8") if claim.request.body is not None else None
    if body is not None:
        headers.setdefault("content-type", "application/octet-stream")
    # The worker is sequential: never reuse response cookies for another notification.
    client.cookies.clear()
    try:
        # HTTPX's per-I/O timeout alone does not stop a peer sending bytes forever.
        async with asyncio.timeout(settings.attempt_timeout_seconds):
            async with client.stream(
                claim.request.method, claim.request.url, headers=headers, content=body,
                follow_redirects=False, timeout=settings.attempt_timeout_seconds,
            ) as response:
                code = response.status_code
                if 200 <= code < 300:
                    return DeliveryResult(True, False, http_status=code)
                retryable = code in {408, 425, 429} or 500 <= code < 600
                return DeliveryResult(
                    False, retryable, code, f"http_{code}", response.headers.get("Retry-After"),
                )
    except TimeoutError:
        return DeliveryResult(False, True, error="attempt_deadline_exceeded")
    except httpx.TransportError as exc:
        return DeliveryResult(False, True, error=type(exc).__name__)


async def run_once(store: Store, client: httpx.AsyncClient, settings: Settings) -> bool:
    claim = store.claim(settings.lease_seconds)
    if claim is None:
        return False
    logger.info("delivery_started id=%s attempt=%s", claim.id, claim.attempt)
    result = await deliver(claim, client, settings)
    now = time.time()
    delay = retry_delay(claim.attempt, result.retry_after, settings, now) if result.retryable else 0
    saved = store.finish(
        claim, success=result.success, retryable=result.retryable,
        http_status=result.http_status, error=result.error, retry_delay=delay,
    )
    if not saved:
        logger.warning("delivery_lease_lost id=%s attempt=%s", claim.id, claim.attempt)
    else:
        logger.info(
            "delivery_finished id=%s attempt=%s success=%s retryable=%s http_status=%s error=%s",
            claim.id, claim.attempt, result.success, result.retryable, result.http_status, result.error,
        )
    return True


async def run(settings: Settings) -> None:
    store = Store(settings.db_path)
    store.initialize()
    logger.info("worker_started")
    async with httpx.AsyncClient(
        trust_env=False, follow_redirects=False,
        limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
    ) as client:
        while True:
            if not await run_once(store, client, settings):
                await asyncio.sleep(settings.poll_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTPX logs full URLs at INFO, which may contain supplier query credentials.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(run(Settings.from_env()))
    except KeyboardInterrupt:
        logger.info("worker_stopped")


if __name__ == "__main__":
    main()
