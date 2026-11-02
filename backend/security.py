"""Same-origin guard: Host allow-list (DNS rebinding) and Origin check (CSRF).

Pure ASGI middleware, so it covers HTTP and WebSocket handshakes alike.
"""

import ipaddress
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
DEFAULT_PORTS = {"http": "80", "https": "443"}


def _split_host(host_header: str) -> str:
    """Hostname without port (handles ``[::1]:8000``), lowercased."""
    h = host_header.strip().lower()
    if h.startswith("["):
        end = h.find("]")
        return h[1:end] if end != -1 else h[1:]
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h


def allowed_hosts_from_env() -> set:
    raw = os.environ.get("VINYLFLOW_ALLOWED_HOSTS", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def host_allowed(host_header: Optional[str], extra=()) -> bool:
    if not host_header:
        return False
    name = _split_host(host_header)
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name in {e.lower() for e in extra}


def _normalize_origin(value: str) -> Optional[str]:
    """``scheme://host[:port]`` with default ports dropped, or None if malformed."""
    scheme, sep, rest = value.strip().lower().partition("://")
    if not sep or not rest or "/" in rest:
        return None
    if scheme in DEFAULT_PORTS and rest.endswith(":" + DEFAULT_PORTS[scheme]):
        rest = rest[: -len(DEFAULT_PORTS[scheme]) - 1]
    return f"{scheme}://{rest}"


def origin_allowed(origin: Optional[str], scheme: str, host_header: Optional[str]) -> bool:
    """No Origin = allowed; otherwise it must equal ``scheme://<Host>``."""
    if origin is None:
        return True
    if not host_header:
        return False
    scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
    own = _normalize_origin(f"{scheme}://{host_header}")
    got = _normalize_origin(origin)  # "null" and junk normalise to None
    return own is not None and got == own


class SameOriginMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        host = headers.get(b"host", b"").decode("latin-1") or None
        origin = headers.get(b"origin")
        origin = origin.decode("latin-1") if origin is not None else None
        method = scope.get("method", "GET") if kind == "http" else "WS"

        reason = None
        if not host_allowed(host, allowed_hosts_from_env()):
            reason, status, body = "Host not allowed", 400, b"Host not allowed"
        elif method not in SAFE_METHODS and not origin_allowed(origin, scope.get("scheme", "http"), host):
            reason, status, body = "Cross-origin request blocked", 403, b"Cross-origin request blocked"

        if reason is None:
            await self.app(scope, receive, send)
            return

        logger.warning("%s: %s %s host=%r origin=%r", reason, method, scope.get("path"), host, origin)
        if kind == "websocket":
            # Consume the connect event, then refuse without accepting.
            await receive()
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
