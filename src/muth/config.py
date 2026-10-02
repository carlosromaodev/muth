from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SCOPES = {"verify", "read", "review", "delete", "audit", "metrics", "feedback", "learning"}


class TenantKey(BaseModel):
    tenant_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    key_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    key_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scopes: set[str]

    @model_validator(mode="after")
    def valid_scopes(self):
        if not self.scopes or not self.scopes <= SCOPES:
            raise ValueError("Scopes inválidos.")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MUTH_", env_file=".env", extra="ignore")

    engine_mode: Literal["disabled", "demo", "biometric"] = "disabled"
    biometric_manifest: str | None = None
    learning_enabled: bool = True
    learning_interval_seconds: int = Field(default=300, ge=10, le=86400)
    # Legacy local key: mandatory if used, scoped exclusively to tenant 'local'.
    api_key: SecretStr = SecretStr("")
    tenants: list[TenantKey] = Field(default_factory=list)
    database_url: str = "sqlite://"
    data_key: SecretStr = SecretStr("")
    max_upload_bytes: int = Field(default=5 * 1024 * 1024, gt=0)
    max_request_bytes: int = Field(default=11 * 1024 * 1024, gt=0)
    max_image_pixels: int = Field(default=12_000_000, gt=0)
    session_ttl_seconds: int = Field(default=1800, ge=1, le=86400)
    retention_days: int = Field(default=30, ge=1, le=365)
    processing_lease_seconds: int = Field(default=120, ge=1, le=3600)
    rate_limit_per_minute: int = Field(default=120, ge=1, le=10000)
    max_inference_requests: int = Field(default=2, ge=1, le=16)
    max_control_request_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)

    @model_validator(mode="after")
    def validate_config(self):
        if not self.database_url.startswith("sqlite:"):
            raise ValueError("Esta versão suporta SQLite; PostgreSQL exige validação específica.")
        from sqlalchemy.engine import make_url

        if make_url(self.database_url).query:
            raise ValueError("Parâmetros SQLite URI não são suportados nesta versão.")
        if not self.in_memory and not self.data_key.get_secret_value():
            raise ValueError("MUTH_DATA_KEY é obrigatória para persistência.")
        if self.data_key.get_secret_value():
            from cryptography.fernet import Fernet

            try:
                Fernet(self.data_key.get_secret_value().encode())
            except (ValueError, TypeError) as exc:
                raise ValueError("MUTH_DATA_KEY deve ser uma chave Fernet válida.") from exc
        if len({item.key_sha256 for item in self.tenants}) != len(self.tenants):
            raise ValueError("Uma chave não pode pertencer a múltiplos principals.")
        if len({item.key_id for item in self.tenants}) != len(self.tenants):
            raise ValueError("key_id deve ser único.")
        if self.api_key.get_secret_value():
            import hashlib

            legacy_hash = hashlib.sha256(self.api_key.get_secret_value().encode()).hexdigest()
            if any(item.key_sha256 == legacy_hash for item in self.tenants):
                raise ValueError("A chave legacy não pode duplicar uma chave de tenant.")
        return self

    @property
    def in_memory(self) -> bool:
        return self.database_url in {"sqlite://", "sqlite:///:memory:"}
