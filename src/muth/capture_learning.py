"""Consented browser contributions, separated from independently reviewed labels.

Candidates contain encrypted document fields and biometric measurements only.
They cannot reach calibration until an authorised reviewer supplies independent
labels and a stable, namespaced subject reference. No images or embeddings are
retained, and OCR corrections proposed by a browser are never trusted labels.
"""

import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Float, String, Text, delete, func, or_, select, text
from sqlalchemy.orm import Mapped, mapped_column

from muth.domain import CreateSession, LearningFeedback, Verification
from muth.errors import MuthError
from muth.learning import collect_samples, erase_samples
from muth.security import Principal, require_scope
from muth.storage import Base, LearningSampleRow, SessionRow

DOCUMENT_FIELD_NAMES = frozenset(
    {
        "name",
        "document_number",
        "birth_date",
        "expiry_date",
        "sex",
        "nationality",
        "parentage",
        "father_name",
        "mother_name",
    }
)
CANONICAL_SUBJECT_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_.-]{1,31}:[a-zA-Z0-9_.:-]{1,94}$"
LEARNING_CONSENT_VERSION = "capture-learning-v1"


def _document_fields(value):
    if not isinstance(value, dict) or len(value) > len(DOCUMENT_FIELD_NAMES):
        raise ValueError("Campos documentais inválidos.")
    if value.keys() - DOCUMENT_FIELD_NAMES:
        raise ValueError("Campo documental não suportado.")
    cleaned = {}
    for name, field in value.items():
        if not isinstance(field, str):
            raise ValueError("A correcção deve ser texto.")
        field = field.strip()
        if not 1 <= len(field) <= 256 or any(
            unicodedata.category(char).startswith("C") for char in field
        ):
            raise ValueError("Texto da correcção inválido.")
        if name in {"birth_date", "expiry_date"}:
            try:
                parsed = datetime.strptime(field, "%Y-%m-%d").date()
            except ValueError as exc:
                raise ValueError("Use datas no formato AAAA-MM-DD.") from exc
            if parsed.isoformat() != field:
                raise ValueError("Use datas no formato AAAA-MM-DD.")
        cleaned[name] = field
    return cleaned


class CaptureLearningInfo(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["not_opted_in", "pending_review", "reviewed", "withdrawn", "unavailable"]
    consented: bool
    samples: int = Field(default=0, ge=0)
    explanation: str


class CaptureDocumentCorrections(BaseModel):
    model_config = {"extra": "forbid"}

    fields: dict[str, str]

    @field_validator("fields", mode="before")
    @classmethod
    def supported_fields(cls, value):
        result = _document_fields(value)
        if not result:
            raise ValueError("Indique pelo menos uma correcção.")
        return result


class CaptureLearningReview(BaseModel):
    model_config = {"extra": "forbid"}

    independent_evidence_confirmed: bool = Field(strict=True)
    subject_reference: str = Field(min_length=4, max_length=128, pattern=CANONICAL_SUBJECT_PATTERN)
    source_reference: str = Field(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    document_fields: dict[str, str] = Field(default_factory=dict)
    face_label: Literal["genuine", "impostor"] | None = None
    liveness_label: Literal["live", "spoof"] | None = None
    capture_subject_reference: str | None = Field(
        default=None, min_length=4, max_length=128, pattern=CANONICAL_SUBJECT_PATTERN
    )
    attack_type: Literal["print", "screen_replay", "mask", "injection", "other"] | None = None

    @field_validator("document_fields", mode="before")
    @classmethod
    def supported_fields(cls, value):
        return _document_fields(value)

    @model_validator(mode="after")
    def independently_reviewed(self):
        if self.independent_evidence_confirmed is not True:
            raise ValueError("A revisão exige evidência independente confirmada.")
        if not self.document_fields and not self.face_label and not self.liveness_label:
            raise ValueError("A revisão deve confirmar campos ou uma label independente.")
        if self.face_label == "impostor" and not self.capture_subject_reference:
            raise ValueError("Par impostor exige sujeito da captura confirmado.")
        if self.face_label == "genuine" and self.capture_subject_reference not in {
            None,
            self.subject_reference,
        }:
            raise ValueError("Referências incompatíveis com a label genuine.")
        if (
            self.face_label == "impostor"
            and self.capture_subject_reference == self.subject_reference
        ):
            raise ValueError("Par impostor exige sujeitos distintos.")
        if (self.liveness_label == "spoof") != (self.attack_type is not None):
            raise ValueError("Identifique o tipo de ataque apenas para a label spoof.")
        return self


class CaptureLearningRow(Base):
    __tablename__ = "capture_learning_candidates"

    session_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)
    retain_until: Mapped[float] = mapped_column(Float, index=True)


def erase_capture_candidates(db, session_ids):
    """Use inside the session deletion/retention transaction; revoke affected policies."""
    ids = list(session_ids)
    if ids:
        erase_samples(db, ids)
        db.execute(delete(CaptureLearningRow).where(CaptureLearningRow.session_id.in_(ids)))


class CaptureLearningService:
    def __init__(self, store, learning):
        self.store, self.learning = store, learning

    def _enabled(self):
        if not self.store.settings.learning_enabled or not getattr(
            self.store.settings, "capture_learning_enabled", True
        ):
            raise MuthError(409, "learning_disabled", "Aprendizagem desactivada.")

    def _portal(self, principal):
        if principal.tenant_id != self.store.settings.capture_tenant_id:
            raise MuthError(404, "session_not_found", "Sessão não encontrada.")

    def _reviewer(self, reviewer):
        require_scope(reviewer, "capture_review")
        return Principal(
            self.store.settings.capture_tenant_id,
            reviewer.key_id,
            frozenset({"capture_review", "read", "feedback"}),
        )

    @staticmethod
    def _begin(store, db):
        if (
            not store.db.in_memory
            and not db.connection().connection.driver_connection.in_transaction
        ):
            db.execute(text("BEGIN IMMEDIATE"))

    def _candidate(self, db, principal, session_id):
        self._portal(principal)
        session = self.store._row(db, principal, session_id)
        candidate = db.scalar(
            select(CaptureLearningRow).where(
                CaptureLearningRow.session_id == session_id,
                CaptureLearningRow.tenant_id == principal.tenant_id,
                CaptureLearningRow.retain_until > self.store.clock(),
            )
        )
        return session, candidate

    def _info(self, db, candidate):
        if candidate is None:
            return CaptureLearningInfo(
                status=(
                    "not_opted_in"
                    if self.store.settings.learning_enabled
                    and getattr(self.store.settings, "capture_learning_enabled", True)
                    else "unavailable"
                ),
                consented=False,
                explanation="Esta verificação não contribui para a aprendizagem.",
            )
        if candidate.state == "withdrawn":
            return CaptureLearningInfo(
                status="withdrawn",
                consented=False,
                explanation="Contributo retirado; dados e amostras de aprendizagem apagados.",
            )
        samples = db.scalar(
            select(func.count())
            .select_from(LearningSampleRow)
            .where(
                LearningSampleRow.session_id == candidate.session_id,
                LearningSampleRow.tenant_id == candidate.tenant_id,
                LearningSampleRow.state.in_({"labelled", "excluded"}),
            )
        )
        reviewed = candidate.state == "reviewed"
        return CaptureLearningInfo(
            status="reviewed" if reviewed else "pending_review",
            consented=True,
            samples=samples,
            explanation=(
                "Contributo revisto; a calibração continua sujeita a testes e limites de risco."
                if reviewed
                else (
                    "Contributo cifrado pendente de revisão independente; "
                    "ainda não treina o sistema."
                )
            ),
        )

    def info(self, principal, session_id):
        with self.store.db.transaction() as db:
            _, candidate = self._candidate(db, principal, session_id)
            return self._info(db, candidate)

    def create(self, principal, session_id, request_id):
        """Record browser opt-in separately until a reviewer establishes the subject."""
        self._enabled()
        with self.store.db.transaction() as db:
            self._begin(self.store, db)
            session, candidate = self._candidate(db, principal, session_id)
            if candidate is not None:
                if candidate.state == "withdrawn":
                    raise MuthError(409, "contribution_withdrawn", "Contributo já retirado.")
                return self._info(db, candidate)
            if session.status != "awaiting_capture" or session.expires_at <= self.store.clock():
                raise MuthError(409, "consent_too_late", "O consentimento precede a captura.")
            candidate = CaptureLearningRow(
                session_id=session_id,
                tenant_id=principal.tenant_id,
                state="awaiting_capture",
                payload=self.store._encrypt(
                    json.dumps(
                        {
                            "consent_version": getattr(
                                self.store.settings,
                                "capture_learning_policy_version",
                                LEARNING_CONSENT_VERSION,
                            )
                        }
                    )
                ),
                created_at=self.store.clock(),
                retain_until=session.retain_until,
            )
            db.add(candidate)
            self.store._event(db, principal, session_id, "capture_learning_opted_in", request_id)
            return self._info(db, candidate)

    def collect(self, principal, session_id, report, request_id):
        """Freeze measurements after completion, but create no calibration samples."""
        with self.store.db.transaction() as db:
            session, candidate = self._candidate(db, principal, session_id)
            if candidate is None or candidate.state == "withdrawn":
                return self._info(db, candidate)
            if candidate.state != "awaiting_capture":
                return self._info(db, candidate)
            if not session.result:
                raise MuthError(409, "capture_pending", "A captura ainda não foi concluída.")
            standard = Verification.model_validate_json(self.store._decrypt(session.result))
            fields = {}
            document_data = getattr(report, "document_data", None)
            if document_data is not None:
                for name, field in document_data.fields.items():
                    if name in DOCUMENT_FIELD_NAMES:
                        fields[name] = field.model_dump(mode="json")
            consent = json.loads(self.store._decrypt(candidate.payload))
            data = {
                "consent_version": consent["consent_version"],
                "document_fields": fields,
                "proposed_fields": {},
                "checks": {
                    role: getattr(standard.checks, key).model_dump(mode="json")
                    for role, key in (("face", "face_match"), ("liveness", "liveness"))
                },
                "review": None,
            }
            candidate.payload = self.store._encrypt(json.dumps(data))
            candidate.state = "pending_review"
            self.store._event(db, principal, session_id, "capture_learning_queued", request_id)
            return self._info(db, candidate)

    def propose(self, principal, session_id, corrections: CaptureDocumentCorrections, request_id):
        with self.store.db.transaction() as db:
            self._begin(self.store, db)
            _, candidate = self._candidate(db, principal, session_id)
            if candidate is None or candidate.state != "pending_review":
                raise MuthError(
                    409, "contribution_not_pending", "Sem contributo pendente de revisão."
                )
            data = json.loads(self.store._decrypt(candidate.payload))
            data["proposed_fields"].update(corrections.fields)
            candidate.payload = self.store._encrypt(json.dumps(data))
            self.store._event(
                db, principal, session_id, "capture_ocr_corrections_proposed", request_id
            )
            return self._info(db, candidate)

    def queue(self, reviewer, limit=50):
        principal = self._reviewer(reviewer)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MuthError(422, "invalid_queue_limit", "Limite de revisão inválido.")
        with self.store.db.transaction() as db:
            rows = db.scalars(
                select(CaptureLearningRow)
                .join(SessionRow, CaptureLearningRow.session_id == SessionRow.session_id)
                .where(
                    CaptureLearningRow.tenant_id == principal.tenant_id,
                    CaptureLearningRow.state == "pending_review",
                    CaptureLearningRow.retain_until > self.store.clock(),
                    self.store._visible(principal, SessionRow.session_id),
                )
                .order_by(CaptureLearningRow.created_at, CaptureLearningRow.session_id)
                .limit(limit)
            )
            return [
                {
                    "session_id": row.session_id,
                    "created_at": datetime.fromtimestamp(row.created_at, UTC).isoformat(),
                    "retain_until": datetime.fromtimestamp(row.retain_until, UTC).isoformat(),
                    **json.loads(self.store._decrypt(row.payload)),
                }
                for row in rows
            ]

    def review(self, reviewer, session_id, review: CaptureLearningReview, request_id):
        self._enabled()
        principal = self._reviewer(reviewer)
        with self.store.db.transaction() as db:
            self._begin(self.store, db)
            session, candidate = self._candidate(db, principal, session_id)
            if candidate is None or candidate.state not in {"pending_review", "reviewed"}:
                raise MuthError(
                    409, "contribution_not_pending", "Sem contributo pendente de revisão."
                )
            data = json.loads(self.store._decrypt(candidate.payload))
            confirmation = review.model_dump(mode="json")
            if candidate.state == "reviewed":
                if data["review"] == confirmation:
                    return self._info(db, candidate)
                raise MuthError(409, "review_conflict", "Revisão confirmada; requer investigação.")
            payload = CreateSession.model_validate_json(self.store._decrypt(session.payload))
            payload.subject_reference = review.subject_reference
            payload.consent.learning_opt_in = True
            session.payload = self.store._encrypt(payload.model_dump_json())
            standard = Verification.model_validate_json(self.store._decrypt(session.result))
            for label, check, expected_kind, minimum in (
                (
                    review.face_label,
                    standard.checks.face_match,
                    "cosine_similarity",
                    -1,
                ),
                (
                    review.liveness_label,
                    standard.checks.liveness,
                    "liveness_softmax",
                    0,
                ),
            ):
                if (
                    label is None
                    or check.score is None
                    or not minimum <= check.score <= 1
                    or check.score_kind != expected_kind
                    or not re.fullmatch(r"[a-f0-9]{64}", check.model_fingerprint or "")
                ):
                    check.model_fingerprint = None
            collect_samples(self.store, db, session, standard)
            db.flush()
            for role, label in (("face", review.face_label), ("liveness", review.liveness_label)):
                if label is None:
                    continue
                sample = db.scalar(
                    select(LearningSampleRow).where(
                        LearningSampleRow.session_id == session_id,
                        LearningSampleRow.tenant_id == principal.tenant_id,
                        LearningSampleRow.role == role,
                    )
                )
                if sample is None:
                    continue  # Demo, missing measurement or unsupported model cannot train.
                feedback = LearningFeedback(
                    role=role,
                    label=label,
                    source="human_review",
                    source_reference=review.source_reference,
                    capture_subject_reference=review.capture_subject_reference,
                    attack_type=review.attack_type if role == "liveness" else None,
                )
                self.learning.feedback(principal, session_id, feedback, request_id)
            data["review"] = confirmation
            candidate.payload = self.store._encrypt(json.dumps(data))
            candidate.state = "reviewed"
            self.store._event(db, principal, session_id, "capture_learning_reviewed", request_id)
            return self._info(db, candidate)

    def withdraw(self, principal, session_id, request_id):
        with self.store.db.transaction() as db:
            self._begin(self.store, db)
            session, candidate = self._candidate(db, principal, session_id)
            if candidate is None:
                return self._info(db, candidate)
            payload = CreateSession.model_validate_json(self.store._decrypt(session.payload))
            payload.consent.learning_opt_in = False
            payload.subject_reference = None
            session.payload = self.store._encrypt(payload.model_dump_json())
            session.retain_until = min(
                session.retain_until,
                self.store.clock() + self.store.settings.capture_retention_days * 86400,
            )
            candidate.retain_until = min(candidate.retain_until, session.retain_until)
            erase_samples(db, [session_id])
            if candidate.state != "withdrawn":
                candidate.state, candidate.payload = "withdrawn", None
                self.store._event(
                    db, principal, session_id, "capture_learning_withdrawn", request_id
                )
            db.flush()
            return self._info(db, candidate)

    def purge(self):
        with self.store.db.transaction() as db:
            ids = list(
                db.scalars(
                    select(CaptureLearningRow.session_id)
                    .outerjoin(SessionRow, CaptureLearningRow.session_id == SessionRow.session_id)
                    .where(
                        or_(
                            CaptureLearningRow.retain_until <= self.store.clock(),
                            SessionRow.session_id.is_(None),
                            SessionRow.status == "deleted",
                            SessionRow.retain_until <= self.store.clock(),
                        )
                    )
                )
            )
            erase_capture_candidates(db, ids)
            return len(ids)
