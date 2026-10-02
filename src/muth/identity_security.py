"""Purpose-separated capabilities for a single retained identity profile."""

import base64
import hmac
import re

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, ValidationError
from starlette._utils import get_route_path
from starlette.concurrency import run_in_threadpool

from muth.capture_security import check_capture_origin
from muth.errors import MuthError
from muth.security import Principal


class IdentityClaims(BaseModel):
    model_config = {"extra": "forbid"}
    audience: str
    identity_id: str = Field(pattern=r"^mth_idn_[a-f0-9]{32}$")
    tenant_id: str
    expires_at: float = Field(allow_inf_nan=False)


class IdentityAccess:
    def __init__(self, store):
        self.store = store
        key = hmac.digest(store.fingerprint_key, b"muth:identity-capability:v1", "sha256")
        self.box = Fernet(base64.urlsafe_b64encode(key))

    def issue(self, identity):
        return self.box.encrypt(
            IdentityClaims(
                audience="muth-identity-v1",
                identity_id=identity.identity_id,
                tenant_id=self.store.settings.capture_tenant_id,
                expires_at=identity.retain_until.timestamp(),
            )
            .model_dump_json()
            .encode()
        ).decode()

    def authenticate(self, authorization, identity_id):
        if (
            not authorization
            or not authorization.startswith("Bearer ")
            or len(authorization) > 2048
        ):
            raise MuthError(401, "invalid_identity_token", "Acesso à identidade inválido.")
        try:
            claims = IdentityClaims.model_validate_json(
                self.box.decrypt(authorization[7:].encode())
            )
        except (InvalidToken, ValidationError, ValueError, UnicodeError) as exc:
            raise MuthError(401, "invalid_identity_token", "Acesso à identidade inválido.") from exc
        if (
            claims.audience != "muth-identity-v1"
            or claims.identity_id != identity_id
            or claims.tenant_id != self.store.settings.capture_tenant_id
        ):
            raise MuthError(401, "invalid_identity_token", "Acesso à identidade inválido.")
        if claims.expires_at <= self.store.clock():
            raise MuthError(410, "identity_token_expired", "O acesso à identidade expirou.")
        return Principal(
            claims.tenant_id, "browser-identity", frozenset({"read", "delete", "verify"})
        )


async def authorize_identity(request):
    path, method = get_route_path(request.scope).rstrip("/"), request.method
    parts = path.split("/")
    valid = (
        len(parts) in {4, 5}
        and parts[:3] == ["", "identity-api", "identities"]
        and bool(re.fullmatch(r"mth_idn_[a-f0-9]{32}", parts[3]))
        and (
            (len(parts) == 4 and method in {"GET", "DELETE"})
            or (len(parts) == 5 and parts[4] == "compare" and method == "POST")
        )
    )
    if not valid:
        raise MuthError(404, "identity_route_not_found", "Operação de identidade indisponível.")
    check_capture_origin(request)
    request.app.state.capture_limiter.check(request.client.host if request.client else "unknown")
    principal = request.app.state.identity_access.authenticate(
        request.headers.get("authorization"), parts[3]
    )
    request.state.identity_principal = principal
    if method == "POST" and request.app.state.settings.engine_mode != "biometric":
        raise MuthError(
            503, "identity_engine_unavailable", "A comparação biométrica está indisponível."
        )
    if not await run_in_threadpool(request.app.state.database.ready):
        raise MuthError(503, "database_not_ready", "Execute a migração da base de dados.")
    await run_in_threadpool(request.app.state.identities.get, principal, parts[3])
