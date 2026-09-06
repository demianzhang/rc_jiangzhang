import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


logger = logging.getLogger(__name__)


class RequestSizeLimit:
    def __init__(self, app: ASGIApp, max_bytes: int = 524288):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                logger.info("request_disconnected")
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                logger.warning("request_too_large")
                response = JSONResponse(status_code=413, content={"detail": "Request exceeds 512 KiB"})
                await response(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                body = b"".join(chunks)
                chunks.clear()
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, send)
