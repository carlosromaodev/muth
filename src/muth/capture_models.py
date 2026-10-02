"""Public, deliberately limited evidence presented by the browser capture flow."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from muth.domain import Check, DocumentCheck, VerificationStatus


class CaptureScore(BaseModel):
    model_config = {"extra": "forbid"}

    value: float | None = Field(default=None, ge=0, le=10, allow_inf_nan=False)
    label: Literal["Nota dos sinais de autenticidade"] = "Nota dos sinais de autenticidade"
    kind: Literal["indicative", "unavailable"]
    authenticity_confirmed: Literal[False] = False
    evidence_ceiling: float = Field(default=6, ge=0, le=10, allow_inf_nan=False)
    explanation: str

    @model_validator(mode="after")
    def consistent_availability(self):
        if (self.value is None) != (self.kind == "unavailable"):
            raise ValueError("Disponibilidade da nota incoerente.")
        if self.value is not None and self.value > self.evidence_ceiling:
            raise ValueError("A nota excede o limite da evidência disponível.")
        return self


class CaptureChecks(BaseModel):
    model_config = {"extra": "forbid"}

    face_match: Check
    liveness: Check
    document_front: DocumentCheck
    document_back: Check


class CaptureVerification(BaseModel):
    model_config = {"extra": "forbid"}

    verification_id: str
    created_at: datetime
    status: VerificationStatus
    mode: str
    score: CaptureScore
    checks: CaptureChecks
    limitations: list[str]
    scoring_version: str
    reasons: list[str]
