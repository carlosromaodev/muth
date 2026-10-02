"""Public identity references and private, consented biometric templates."""

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from muth.document_models import DocumentData

FACE_TEMPLATE_DIMENSION = 128


class EnrollmentConsent(BaseModel):
    model_config = {"extra": "forbid"}

    accepted: bool = Field(strict=True)
    purpose: Literal["identity_enrollment"] = "identity_enrollment"
    policy_version: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")

    @field_validator("accepted")
    @classmethod
    def explicit_acceptance(cls, value):
        if value is not True:
            raise ValueError("Consentimento explícito para guardar a identidade é obrigatório.")
        return value


class IdentityView(BaseModel):
    """No embedding, stable person hash or claimed identity approval is public."""

    model_config = {"extra": "forbid"}

    identity_id: str = Field(pattern=r"^mth_idn_[a-f0-9]{32}$")
    source_session_id: str = Field(pattern=r"^mth_ses_[a-f0-9]{32}$")
    status: Literal["provisional"] = "provisional"
    document_data: DocumentData
    created_at: datetime
    retain_until: datetime
    authenticity_confirmed: Literal[False] = False
    biometric_template_saved: Literal[True] = True


class IdentityPayload(BaseModel):
    """Encrypted storage format; never use this model as an API response."""

    model_config = {"extra": "forbid"}

    version: Literal[1] = 1
    embedding: list[float] = Field(
        min_length=FACE_TEMPLATE_DIMENSION, max_length=FACE_TEMPLATE_DIMENSION
    )
    model_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    dimension: Literal[128] = FACE_TEMPLATE_DIMENSION
    document_data: DocumentData
    consent: EnrollmentConsent

    @field_validator("embedding", mode="before")
    @classmethod
    def normalised_finite_vector(cls, value):
        if not isinstance(value, list) or len(value) != FACE_TEMPLATE_DIMENSION:
            raise ValueError("Dimensão do template facial inválida.")
        if any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            or abs(item) > 1.00001
            for item in value
        ):
            raise ValueError("Template facial contém valores inválidos.")
        norm = math.sqrt(math.fsum(float(item) ** 2 for item in value))
        if not math.isclose(norm, 1.0, abs_tol=0.001):
            raise ValueError("O template facial tem de estar normalizado.")
        # Store a deterministic, precisely normalised representation after validation.
        return [float(item) / norm for item in value]
