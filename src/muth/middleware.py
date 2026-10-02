import threading
import time
from uuid import uuid4

from fastapi import Request
from prometheus_client import CollectorRegistry, Counter, Histogram
from starlette.responses import JSONResponse

from muth.errors import MuthError
from muth.security import authenticate, require_scope


class RateLimiter:
    """Per-process fixed window; multiple replicas require a shared gateway."""

    def __init__(self, limit: int, *, clock=time.monotonic):
        self.limit = limit
        self.clock = clock
        self.windows: dict[str, tuple[int, int]] = {}
        self.lock = threading.Lock()

    def check(self, key_id: str):
        now = int(self.clock() // 60)
        with self.lock:
            window, count = self.windows.get(key_id, (now, 0))
            count = count if window == now else 0
            if count >= self.limit:
                raise MuthError(429, "rate_limit_exceeded", "Limite de pedidos atingido.")
            self.windows[key_id] = (now, count + 1)


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "muth_http_requests_total",
            "HTTP requests",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "muth_http_duration_seconds",
            "Request duration",
            ["method", "route"],
            registry=self.registry,
        )


def error_response(status, code, message, request_id):
    return JSONResponse(
        status_code=status,
        headers={"Retry-After": "60"} if status == 429 else None,
        content={"error": {"code": code, "message": message}, "request_id": request_id},
    )


def operation_scope(path: str, method: str) -> str:
    if path == "/metrics":
        return "metrics"
    if path.startswith("/v1/learning"):
        return "learning"
    if path.endswith("/feedback"):
        return "feedback"
    if path.endswith("/learning-consent"):
        return "delete"
    if path.endswith("/events"):
        return "audit"
    if path.endswith("/review"):
        return "review"
    if method == "DELETE":
        return "delete"
    return "read" if method == "GET" else "verify"


class PlatformMiddleware:
    def __init__(self, app, *, settings, metrics):
        self.app = app
        self.settings = settings
        self.metrics = metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 500
        response_started = False

        async def traced_send(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-request-id", request_id.encode()),
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        try:
            path = scope["path"]
            if path == "/metrics" or path == "/v1" or path.startswith("/v1/"):
                request = Request(scope)
                principal = authenticate(request, request.headers.get("x-api-key"))
                require_scope(principal, operation_scope(path, scope["method"]))
            # Bound before multipart parsing, including bodies without Content-Length.
            headers = dict(scope.get("headers", []))
            if b"content-length" in headers:
                try:
                    declared = int(headers[b"content-length"])
                except ValueError as exc:
                    raise MuthError(
                        400, "invalid_content_length", "Content-Length inválido."
                    ) from exc
                if declared < 0:
                    raise MuthError(400, "invalid_content_length", "Content-Length inválido.")
                if declared > self.settings.max_request_bytes:
                    raise MuthError(413, "request_too_large", "Corpo do pedido excede o limite.")
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > self.settings.max_request_bytes:
                    raise MuthError(413, "request_too_large", "Corpo do pedido excede o limite.")
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            consumed = False

            async def bounded_receive():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, bounded_receive, traced_send)
        except MuthError as exc:
            await error_response(exc.status, exc.code, exc.message, request_id)(
                scope, receive, traced_send
            )
        except Exception:
            if response_started:
                raise
            await error_response(
                500, "internal_error", "Não foi possível concluir o pedido.", request_id
            )(scope, receive, traced_send)
        finally:
            route = scope.get("route")
            label = route.path if route is not None and hasattr(route, "path") else "unmatched"
            method = (
                scope["method"]
                if scope["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                else "OTHER"
            )
            self.metrics.requests.labels(method, label, str(status)).inc()
            self.metrics.latency.labels(method, label).observe(time.monotonic() - started)
