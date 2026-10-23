"""Small ASGI middlewares: response security headers and a request body cap."""

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings

# hamdoor is never meant to be framed (a framed page keeps the session and
# could be clicked through, e.g. to switch map privacy); scripts and styles
# are served with their real types; other sites get the origin only (map
# tile servers ask for a Referer)
SECURITY_HEADERS = (
    (b"content-security-policy", b"frame-ancestors 'none'; base-uri 'self'; object-src 'none'"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in SECURITY_HEADERS if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodySizeLimitMiddleware:
    """413 for a request body over settings.max_request_bytes: by its
    Content-Length before anything is read, or while a chunked body streams
    in (raised from receive(), which FastAPI turns into the response)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = get_settings().max_request_bytes
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > limit:
                    await _send_too_large(send, limit)
                    return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, _too_large_text(limit))
            return message

        await self.app(scope, limited_receive, send)


def _too_large_text(limit: int) -> str:
    return f"request body is larger than {limit // 1024} KB"


async def _send_too_large(send: Send, limit: int) -> None:
    body = ('{"detail":"' + _too_large_text(limit) + '"}').encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close"), *SECURITY_HEADERS]})
    await send({"type": "http.response.body", "body": body})
