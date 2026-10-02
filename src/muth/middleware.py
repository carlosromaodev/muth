import asyncio
import threading
import time
from uuid import uuid4

from fastapi import Request
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from starlette._utils import get_route_path
from starlette.responses import JSONResponse

from muth.capacity import InferenceCapacity
from muth.capture_security import authorize_capture
from muth.errors import MuthError
from muth.security import authenticate, require_scope

WEB_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; "
        "font-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(self), microphone=(), geolocation=()",
    "X-Frame-Options": "DENY",
}


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
        self.inference_requests = Gauge(
            "muth_inference_requests_inflight",
            "Admitted inference HTTP requests",
            registry=self.registry,
        )
        self.inference_workers = Gauge(
            "muth_inference_workers_active", "Synchronous inference workers", registry=self.registry
        )
        self.capacity_rejections = Counter(
            "muth_inference_capacity_rejections_total",
            "Inference capacity rejections",
            registry=self.registry,
        )


def error_response(status, code, message, request_id):
    return JSONResponse(
        status_code=status,
        headers={"Retry-After": "1" if code == "inference_capacity_exceeded" else "60"}
        if status == 429
        else None,
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


def is_inference_request(path, method):
    if method != "POST":
        return False
    path = path.rstrip("/")
    if path in {"/v1/verifications", "/v1/faces/compare", "/v1/liveness", "/v1/documents/analyze"}:
        return True
    parts = path.split("/")
    if len(parts) == 5 and parts[:3] == ["", "capture-api", "sessions"]:
        return bool(parts[3]) and parts[4] == "verify"
    return (
        len(parts) == 5
        and parts[:3] == ["", "v1", "sessions"]
        and bool(parts[3])
        and parts[4] == "verify"
    )


class PlatformMiddleware:
    def __init__(self, app, *, settings, metrics, capacity=None):
        self.app = app
        self.settings = settings
        self.metrics = metrics
        self.capacity = capacity or InferenceCapacity(settings.max_inference_requests, metrics)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status = 500
        response_started = False
        admitted = False
        request_path = get_route_path(scope)

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
                web_path = request_path
                if web_path in {"/", "/capture"} or web_path.startswith("/assets/"):
                    present = {name.lower() for name, _ in headers}
                    headers.extend(
                        (name.lower().encode(), value.encode())
                        for name, value in WEB_SECURITY_HEADERS.items()
                        if name.lower().encode() not in present
                    )
                message = {**message, "headers": headers}
            await send(message)

        try:
            path = request_path
            request = Request(scope)
            if path == "/metrics" or path == "/v1" or path.startswith("/v1/"):
                principal = authenticate(request, request.headers.get("x-api-key"))
                require_scope(principal, operation_scope(path, scope["method"]))
            if path == "/capture-api" or path.startswith("/capture-api/"):
                await authorize_capture(request)
            inference = is_inference_request(path, scope["method"])
            if inference:
                admitted = self.capacity.acquire_request()
                if not admitted:
                    raise MuthError(
                        429, "inference_capacity_exceeded", "Capacidade de inferência ocupada."
                    )
            limit = (
                self.settings.capture_max_request_bytes
                if inference and path.startswith("/capture-api/")
                else self.settings.max_request_bytes
                if inference
                else min(self.settings.max_request_bytes, self.settings.max_control_request_bytes)
            )
            # Bound before multipart parsing, including bodies without Content-Length.
            headers = dict(scope.get("headers", []))
            lengths = [
                value
                for name, value in scope.get("headers", [])
                if name.lower() == b"content-length"
            ]
            declared = None
            if len(lengths) > 1 or (lengths and b"transfer-encoding" in headers):
                raise MuthError(400, "ambiguous_body_length", "Cabeçalhos de comprimento ambíguos.")
            if b"content-length" in headers:
                try:
                    declared = int(headers[b"content-length"])
                except ValueError as exc:
                    raise MuthError(
                        400, "invalid_content_length", "Content-Length inválido."
                    ) from exc
                if declared < 0:
                    raise MuthError(400, "invalid_content_length", "Content-Length inválido.")
                if declared > limit:
                    raise MuthError(413, "request_too_large", "Corpo do pedido excede o limite.")
            body = bytearray()
            deadline = time.monotonic() + self.settings.request_body_timeout_seconds
            while True:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    message = await asyncio.wait_for(receive(), timeout=remaining)
                except TimeoutError as exc:
                    raise MuthError(
                        408, "request_body_timeout", "O envio demorou demasiado. Repita."
                    ) from exc
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if len(body) + len(chunk) > limit:
                    raise MuthError(413, "request_too_large", "Corpo do pedido excede o limite.")
                body.extend(chunk)
                if not message.get("more_body", False):
                    break
            if declared is not None and declared != len(body):
                raise MuthError(400, "body_length_mismatch", "Comprimento do corpo inválido.")
            if inference and path.startswith("/capture-api/"):
                request.app.state.capture_access.authenticate(
                    request.headers.get("authorization"), path.split("/")[3]
                )
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
            if admitted:
                self.capacity.release_request()
            route = scope.get("route")
            label = route.path if route is not None and hasattr(route, "path") else "unmatched"
            method = (
                scope["method"]
                if scope["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                else "OTHER"
            )
            self.metrics.requests.labels(method, label, str(status)).inc()
            self.metrics.latency.labels(method, label).observe(time.monotonic() - started)
