"""Eingehender Bearer-Token-Schutz (reine ASGI-Middleware, /healthz ausgenommen)."""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable, Sequence

ASGIApp = Callable[[dict, Callable, Callable], Awaitable[None]]


def _bearer_from_headers(headers: Sequence[tuple[bytes, bytes]]) -> str:
    for name, value in headers:
        if name == b"authorization":
            decoded = value.decode("latin-1").strip()
            scheme, _, token = decoded.partition(" ")
            if scheme.lower() == "bearer":
                return token.strip()
    return ""


def _authorized(header_token: str, allowed_tokens: Sequence[str]) -> bool:
    if not header_token:
        return False
    for candidate in allowed_tokens:
        if hmac.compare_digest(header_token.encode("utf-8"), candidate.encode("utf-8")):
            return True
    return False


class BearerAuthMiddleware:
    """Prüft `Authorization: Bearer <token>` gegen MCP_AUTH_TOKENS.

    - `GET /healthz` ist ohne Auth erreichbar (Health-Check).
    - Alle anderen Pfade brauchen ein gültiges Token, sonst 401 mit
      `WWW-Authenticate: Bearer`.
    """

    def __init__(self, app: ASGIApp, allowed_tokens: Sequence[str]) -> None:
        self.app = app
        self._allowed = [t for t in allowed_tokens if t]

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/healthz":
            await self.app(scope, receive, send)
            return

        token = _bearer_from_headers(scope.get("headers", []))
        if _authorized(token, self._allowed):
            await self.app(scope, receive, send)
            return

        body = json.dumps({"error": "Unauthorized: gültiger Bearer-Token erforderlich."}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", b"Bearer"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
