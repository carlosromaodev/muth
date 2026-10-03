from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class VerificationStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REVIEW = "review"


class Check(BaseModel):
    outcome: Outcome
    reasons: list[str]
    provider: str
    model_version: str
    # Provider-specific measurement; never interpreted as an identity probability.
    score: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    score_kind: str | None = None
    model_fingerprint: str | None = None
    calibration_version: str | None = None
    threshold: float | None = Field(default=None, ge=-1, le=1, allow_inf_nan=False)
    diagnostics: dict[str, float] = Field(default_factory=dict)


class DocumentCheck(Check):
    document_type: str | None = None
    issuing_country: str | None = None
    fields: dict[str, str] = Field(default_factory=dict)


class Checks(BaseModel):
    document: DocumentCheck
    liveness: Check
    face_match: Check


class Verification(BaseModel):
    verification_id: str
    created_at: datetime
    status: VerificationStatus
    mode: str
    policy_version: str
    reasons: list[str]
    checks: Checks


class SessionStatus(StrEnum):
    AWAITING_CAPTURE = "awaiting_capture"
    PROCESSING = "processing"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Consent(BaseModel):
    model_config = {"extra": "forbid"}
    accepted: bool = Field(strict=True)
    learning_opt_in: bool = Field(default=False, strict=True)
    camera_frames_opt_in: bool = Field(default=False, strict=True)
    camera_policy_version: str | None = Field(default=None, min_length=1, max_length=80)
    purpose: Literal["onboarding", "account_recovery", "step_up"]
    policy_version: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")

    @model_serializer(mode="wrap")
    def serialize_consent(self, handler):
        result = handler(self)
        # Legacy sessions have no permission to stream frames. Preserve their
        # established response shape while validation restores safe defaults.
        if not self.camera_frames_opt_in and self.camera_policy_version is None:
            result.pop("camera_frames_opt_in", None)
            result.pop("camera_policy_version", None)
        return result

    @field_validator("accepted")
    @classmethod
    def accepted_consent(cls, value):
        if value is not True:
            raise ValueError("Consentimento explícito é obrigatório.")
        return value


class CreateSession(BaseModel):
    model_config = {"extra": "forbid"}
    consent: Consent
    subject_reference: str | None = Field(default=None, min_length=1, max_length=128)
    device_group: Literal["android_entry", "android_modern", "ios", "web", "unknown"] = "unknown"

    @model_validator(mode="after")
    def learning_subject(self):
        if self.consent.learning_opt_in and not self.subject_reference:
            raise ValueError("Aprendizagem exige referência de sujeito estável e pseudónima.")
        return self


class SessionView(BaseModel):
    session_id: str
    status: SessionStatus
    created_at: datetime
    expires_at: datetime
    retain_until: datetime
    consent: Consent
    subject_reference: str | None = None
    device_group: str = "unknown"
    verification: Verification | None = None


class ReviewDecision(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["rejected"]
    reason_code: Literal[
        "suspected_fraud", "unreadable_document", "identity_mismatch", "insufficient_evidence"
    ]


class AuditEvent(BaseModel):
    event_id: str
    session_id: str
    event_type: str
    actor: str
    created_at: datetime
    request_id: str


class LearningFeedback(BaseModel):
    model_config = {"extra": "forbid"}
    role: Literal["face", "liveness"]
    label: Literal["genuine", "impostor", "live", "spoof"]
    source: Literal["human_review", "official_verified"] = "human_review"
    source_reference: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    capture_subject_reference: str | None = Field(default=None, min_length=1, max_length=128)
    attack_type: Literal["print", "screen_replay", "mask", "injection", "other"] | None = None

    @model_validator(mode="after")
    def label_contract(self):
        if self.role == "face":
            if self.label not in {"genuine", "impostor"} or self.attack_type:
                raise ValueError("Label facial inválida.")
            if self.label == "impostor" and not self.capture_subject_reference:
                raise ValueError("Par impostor exige referência confirmada do sujeito da captura.")
        elif self.label not in {"live", "spoof"}:
            raise ValueError("Label de liveness inválida.")
        elif (self.label == "spoof") != (self.attack_type is not None):
            raise ValueError("Ataque deve ser identificado para amostras spoof.")
        return self
