"""Purpose-bound browser capabilities, separate from backend API credentials."""

import base64
import hashlib
import hmac
import threading
import time
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request
from pydantic import BaseModel, Field, ValidationError
from starlette._utils import get_route_path
from starlette.concurrency import run_in_threadpool

from muth.errors import MuthError
from muth.security import Principal


class CaptureClaims(BaseModel):
    model_config = {"extra": "forbid"}
    audience: str
    session_id: str = Field(pattern=r"^mth_ses_[a-f0-9]{32}$")
    tenant_id: str
    expires_at: float = Field(allow_inf_nan=False)


class CaptureAccess:
    def __init__(self, store):
        self.store = store
        key = hmac.digest(store.fingerprint_key, b"muth:browser-capability:v1", "sha256")
        self.box = Fernet(base64.urlsafe_b64encode(key))

    def issue(self, session) -> str:
        claims = CaptureClaims(
            audience="muth-capture-v1",
            session_id=session.session_id,
            tenant_id=self.store.settings.capture_tenant_id,
            expires_at=session.retain_until.timestamp(),
        )
        return self.box.encrypt(claims.model_dump_json().encode()).decode()

    def authenticate(self, authorization: str | None, session_id: str) -> Principal:
        if (
            not authorization
            or not authorization.startswith("Bearer ")
            or len(authorization) > 2048
        ):
            raise MuthError(401, "invalid_capture_token", "Ligação de captura inválida.")
        try:
            claims = CaptureClaims.model_validate_json(
                self.box.decrypt(authorization.removeprefix("Bearer ").encode())
            )
        except (InvalidToken, ValidationError, ValueError, UnicodeError) as exc:
            raise MuthError(401, "invalid_capture_token", "Ligação de captura inválida.") from exc
        if (
            claims.audience != "muth-capture-v1"
            or claims.session_id != session_id
            or claims.tenant_id != self.store.settings.capture_tenant_id
        ):
            raise MuthError(401, "invalid_capture_token", "Ligação de captura inválida.")
        if claims.expires_at <= self.store.clock():
            raise MuthError(410, "capture_token_expired", "O prazo da captura terminou. Recomece.")
        return Principal(
            claims.tenant_id, "browser-capture", frozenset({"verify", "read", "delete"})
        )


def check_capture_origin(request: Request) -> None:
    if request.method not in {"POST", "DELETE"}:
        return
    site = request.headers.get("sec-fetch-site")
    if site and site not in {"same-origin", "none"}:
        raise MuthError(403, "capture_origin_denied", "Abra a captura no endereço da MUTH.")
    origin = request.headers.get("origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            expected = urlsplit(str(request.base_url))
        except ValueError as exc:
            raise MuthError(403, "capture_origin_denied", "Origem de captura inválida.") from exc
        if (
            parsed.scheme != expected.scheme
            or parsed.netloc.lower() != expected.netloc.lower()
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
        ):
            raise MuthError(403, "capture_origin_denied", "Abra a captura no endereço da MUTH.")


async def authorize_capture(request: Request) -> None:
    path, method = get_route_path(request.scope).rstrip("/"), request.method
    parts = path.split("/")
    config = path == "/capture-api/config" and method == "GET"
    create = path == "/capture-api/sessions" and method == "POST"
    session_route = (
        len(parts) in {4, 5}
        and parts[:3] == ["", "capture-api", "sessions"]
        and bool(parts[3])
        and (
            (len(parts) == 4 and method in {"GET", "DELETE"})
            or (
                len(parts) == 5
                and (
                    (parts[4] in {"verify", "document-corrections"} and method == "POST")
                    or (parts[4] == "learning-consent" and method == "DELETE")
                )
            )
        )
    )
    if not (config or create or session_route):
        raise MuthError(404, "capture_route_not_found", "Operação de captura indisponível.")
    settings = request.app.state.settings
    collecting = create or (session_route and method == "POST")
    if collecting and (not settings.capture_portal_enabled or settings.engine_mode == "disabled"):
        raise MuthError(503, "capture_unavailable", "A captura está temporariamente indisponível.")
    check_capture_origin(request)
    address = request.client.host if request.client else "unknown"
    request.app.state.capture_limiter.check(address)
    if create:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise MuthError(415, "capture_json_required", "O consentimento exige JSON.")
        request.app.state.capture_limiter.check(address, creation=True)
    if session_route and len(parts) == 5 and parts[4] == "document-corrections":
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise MuthError(415, "capture_json_required", "As correcções exigem JSON.")
    if session_route:
        principal = request.app.state.capture_access.authenticate(
            request.headers.get("authorization"), parts[3]
        )
        request.state.capture_principal = principal

    if not config:

        def ready_and_visible():
            if not request.app.state.database.ready():
                raise MuthError(503, "database_not_ready", "Execute a migração da base de dados.")
            if session_route:
                session = request.app.state.store.get(principal, parts[3])
                if (
                    method == "POST"
                    and len(parts) == 5
                    and parts[4] == "verify"
                    and session.expires_at.timestamp() <= request.app.state.store.clock()
                ):
                    raise MuthError(
                        410, "session_expired", "O prazo da captura terminou. Recomece."
                    )

        await run_in_threadpool(ready_and_visible)


class CaptureRateLimiter:
    """Bounded per-process windows with pseudonymous addresses and a global ceiling."""

    def __init__(self, settings, key: bytes, *, clock=time.monotonic):
        self.settings, self.key, self.clock = settings, key, clock
        self.windows: dict[str, tuple[int, int]] = {}
        self.lock = threading.Lock()

    def check(self, address: str, *, creation: bool = False):
        digest = hmac.digest(self.key, address.encode(), "sha256").hex()
        lane = "create" if creation else "request"
        limit = (
            self.settings.capture_creation_limit_per_minute
            if creation
            else self.settings.capture_rate_limit_per_minute
        )
        keys = [(f"global:{lane}", limit * 4), (f"{lane}:{digest}", limit)]
        now = int(self.clock() // 60)
        with self.lock:
            # Cleanup every window transition; never grow by attacker-supplied addresses forever.
            self.windows = {key: value for key, value in self.windows.items() if value[0] == now}
            if len(self.windows) >= 2048 and any(key not in self.windows for key, _ in keys):
                raise MuthError(429, "rate_limit_exceeded", "Limite de pedidos atingido.")
            for key, ceiling in keys:
                if self.windows.get(key, (now, 0))[1] >= ceiling:
                    raise MuthError(429, "rate_limit_exceeded", "Limite de pedidos atingido.")
            for key, _ in keys:
                self.windows[key] = (now, self.windows.get(key, (now, 0))[1] + 1)


def capture_fingerprint(store, principal, session_id, *images) -> str:
    digest = hmac.new(store.fingerprint_key, digestmod=hashlib.sha256)
    for value in (b"muth:three-captures:v1", principal.tenant_id.encode(), session_id.encode()):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    for image in images:
        digest.update(len(image.content).to_bytes(8, "big"))
        digest.update(image.content)
    return digest.hexdigest()
