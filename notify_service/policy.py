import re
from urllib.parse import urlsplit

import httpx


HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9a-zA-Z]+$")
BLOCKED_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection",
    "proxy-authorization", "proxy-connection", "upgrade", "keep-alive",
    "te", "trailer", "x-notification-id",
}


def normalize_url(value: str) -> tuple[str, str]:
    if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("URL must be ASCII without whitespace; percent-encode other characters")
    if "\\" in value or "#" in value:
        raise ValueError("URL must not contain backslashes or fragments")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only absolute HTTP(S) URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not supported; use headers")
    try:
        url = httpx.URL(value)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (ValueError, httpx.InvalidURL) as exc:
        raise ValueError("Invalid URL") from exc
    if not 1 <= port <= 65535 or parsed.port == 0:
        raise ValueError("Invalid URL port")
    normalized = str(url)
    # Use the HTTP client's interpretation for both the allowlist and delivery.
    origin = str(url.copy_with(raw_path=b"/")).rstrip("/")
    return normalized, origin


def normalize_origin(value: str) -> str:
    _, origin = normalize_url(value)
    parsed = urlsplit(value)
    if parsed.path not in {"", "/"} or parsed.query:
        raise ValueError("Allowed origins must not include a path or query")
    return origin


def normalize_headers(headers: dict[str, str]) -> dict[str, str]:
    if len(headers) > 32:
        raise ValueError("At most 32 headers are allowed")
    normalized: dict[str, str] = {}
    size = 0
    for name, value in headers.items():
        lower = name.lower()
        if not HEADER_NAME.fullmatch(name) or lower in BLOCKED_HEADERS:
            raise ValueError("Invalid or reserved header name")
        if lower in normalized:
            raise ValueError("Header names must be unique ignoring case")
        if any(ord(char) < 32 or ord(char) > 126 for char in value):
            raise ValueError("Header values must be printable ASCII")
        if value != value.strip(" "):
            raise ValueError("Header values must not start or end with spaces")
        size += len(name) + len(value)
        normalized[lower] = value
    if size > 8192:
        raise ValueError("Headers exceed 8192 bytes")
    return normalized
