import hashlib
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from muth.config import SCOPES
from muth.errors import MuthError

api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    key_id: str
    scopes: frozenset[str]


def authenticate(
    request: Request, x_api_key: Annotated[str | None, Security(api_key_scheme)] = None
) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    if not x_api_key or len(x_api_key) > 512:
        raise MuthError(401, "invalid_api_key", "API key inválida ou ausente.")
    supplied = hashlib.sha256(x_api_key.encode()).hexdigest()
    settings = request.app.state.settings
    matched = None
    for key in settings.tenants:
        if secrets.compare_digest(supplied, key.key_sha256):
            matched = Principal(key.tenant_id, key.key_id, frozenset(key.scopes))
    legacy = settings.api_key.get_secret_value()
    if legacy and secrets.compare_digest(supplied, hashlib.sha256(legacy.encode()).hexdigest()):
        matched = Principal("local", "legacy-local", frozenset(SCOPES - {"capture_review"}))
    if matched is None:
        raise MuthError(401, "invalid_api_key", "API key inválida ou ausente.")
    request.app.state.limiter.check(matched.key_id)
    request.state.principal = matched
    return matched


def require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise MuthError(
            403, "insufficient_scope", "Esta chave não tem autorização para a operação."
        )
