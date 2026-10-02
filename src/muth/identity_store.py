"""Tenant-scoped encrypted profiles; enrolment is not identity authentication."""

from datetime import UTC, datetime
from uuid import uuid4

from cryptography.fernet import InvalidToken
from pydantic import ValidationError
from sqlalchemy import Float, Integer, String, Text, UniqueConstraint, and_, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from muth.capture_models import CaptureVerification
from muth.document_models import DocumentData
from muth.domain import Outcome, Verification
from muth.errors import MuthError
from muth.identity_models import EnrollmentConsent, IdentityPayload, IdentityView
from muth.security import Principal
from muth.storage import Base, SessionRow, SessionStore


class IdentityRow(Base):
    __tablename__ = "identity_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "source_session_id"),)

    identity_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    source_session_id: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(32))
    model_fingerprint: Mapped[str] = mapped_column(String(64))
    dimension: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)
    retain_until: Mapped[float] = mapped_column(Float, index=True)


def erase_session_identities(db, session_ids: list[str]) -> int:
    """Explicit session deletion withdraws associated enrolment consent as well."""
    if not session_ids:
        return 0
    changed = db.execute(
        update(IdentityRow)
        .where(IdentityRow.source_session_id.in_(session_ids), IdentityRow.status != "deleted")
        .values(status="deleted", payload=None)
    ).rowcount
    db.execute(
        update(SessionRow)
        .where(SessionRow.session_id.in_(session_ids))
        .values(enrollment_consent=None)
    )
    return changed


# Retain the descriptive name used by integrations while sharing one implementation.
delete_session_identities = erase_session_identities


class IdentityStore:
    def __init__(self, store: SessionStore):
        self.store = store

    def _visible(self, principal: Principal, identity_id: str):
        return and_(
            IdentityRow.identity_id == identity_id,
            IdentityRow.tenant_id == principal.tenant_id,
            IdentityRow.status == "provisional",
            IdentityRow.payload.is_not(None),
            IdentityRow.retain_until > self.store.clock(),
        )

    def _row(self, db, principal: Principal, identity_id: str):
        row = db.scalar(select(IdentityRow).where(self._visible(principal, identity_id)))
        if row is None:
            raise MuthError(404, "identity_not_found", "Identidade não encontrada.")
        return row

    def _payload(self, row: IdentityRow) -> IdentityPayload:
        try:
            payload = IdentityPayload.model_validate_json(self.store._decrypt(row.payload))
        except (InvalidToken, ValidationError, ValueError, UnicodeError) as exc:
            raise MuthError(
                503, "identity_template_invalid", "Template facial indisponível."
            ) from exc
        if payload.model_fingerprint != row.model_fingerprint or payload.dimension != row.dimension:
            raise MuthError(503, "identity_template_invalid", "Template facial indisponível.")
        return payload

    def _view(self, row: IdentityRow) -> IdentityView:
        payload = self._payload(row)
        return IdentityView(
            identity_id=row.identity_id,
            source_session_id=row.source_session_id,
            document_data=payload.document_data,
            created_at=datetime.fromtimestamp(row.created_at, UTC),
            retain_until=datetime.fromtimestamp(row.retain_until, UTC),
        )

    def _consent(self, session: SessionRow) -> EnrollmentConsent:
        if not session.enrollment_consent:
            raise MuthError(
                409, "enrollment_consent_required", "Falta consentimento para guardar a identidade."
            )
        try:
            consent = EnrollmentConsent.model_validate_json(
                self.store._decrypt(session.enrollment_consent)
            )
        except (InvalidToken, ValidationError, ValueError, UnicodeError) as exc:
            raise MuthError(
                409, "enrollment_consent_invalid", "O consentimento de identidade é inválido."
            ) from exc
        if consent.policy_version != self.store.settings.identity_enrollment_policy_version:
            raise MuthError(
                409, "enrollment_consent_invalid", "A política de identidade foi actualizada."
            )
        return consent

    def _source(self, session: SessionRow, document_data: DocumentData, fingerprint: str):
        if not session.result or not session.capture_result or session.status == "processing":
            raise MuthError(
                409, "capture_pending", "Conclua a verificação antes de guardar a identidade."
            )
        standard = Verification.model_validate_json(self.store._decrypt(session.result))
        report = CaptureVerification.model_validate_json(
            self.store._decrypt(session.capture_result)
        )
        if standard.mode not in {"biometric", "configured"} or report.mode not in {
            "biometric",
            "configured",
        }:
            raise MuthError(
                409, "identity_unavailable", "Este modo não produz templates de identidade."
            )
        if (
            standard.status == "rejected"
            or report.status == "rejected"
            or any(
                check.outcome == Outcome.FAIL
                for check in (
                    standard.checks.face_match,
                    standard.checks.liveness,
                    standard.checks.document,
                )
            )
        ):
            raise MuthError(
                409, "identity_evidence_failed", "A captura não permite guardar a identidade."
            )
        face, pad = standard.checks.face_match, standard.checks.liveness
        if (
            face.score is None
            or pad.score is None
            or face.score_kind != "cosine_similarity"
            or pad.score_kind != "liveness_softmax"
            or face.score < 0.363
            or pad.score < 0.8
        ):
            raise MuthError(
                409,
                "identity_evidence_insufficient",
                "A captura não tem sinais biométricos suficientes.",
            )
        if standard.checks.face_match.model_fingerprint != fingerprint:
            raise MuthError(
                409, "identity_model_conflict", "O modelo facial difere da captura original."
            )
        if not document_data.fields or document_data.status == "unavailable":
            raise MuthError(409, "document_data_missing", "Faltam dados legíveis da credencial.")
        if report.document_data != document_data:
            raise MuthError(
                409, "identity_document_conflict", "Os dados diferem da credencial analisada."
            )

    def enroll(
        self,
        principal: Principal,
        session_id: str,
        document_data: DocumentData,
        embedding: list[float],
        model_fingerprint: str,
        request_id: str,
    ) -> IdentityView:
        if not self.store.settings.identity_enrollment_enabled:
            raise MuthError(
                409, "identity_enrollment_disabled", "O registo de identidades está desactivado."
            )
        if self.store.settings.engine_mode != "biometric":
            raise MuthError(
                409, "identity_unavailable", "Este modo não produz templates de identidade."
            )
        with self.store.db.transaction() as db:
            # Serialise new profiles across SQLite processes, not only Python threads.
            connection = db.connection()
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            session = self.store._row(db, principal, session_id)
            consent = self._consent(session)
            try:
                payload = IdentityPayload(
                    embedding=embedding,
                    model_fingerprint=model_fingerprint,
                    document_data=document_data,
                    consent=consent,
                )
            except ValidationError as exc:
                raise MuthError(
                    422, "identity_template_invalid", "Template facial inválido."
                ) from exc
            self._source(session, document_data, model_fingerprint)
            existing = db.scalar(
                select(IdentityRow).where(
                    IdentityRow.tenant_id == principal.tenant_id,
                    IdentityRow.source_session_id == session_id,
                )
            )
            if existing is not None:
                if existing.status == "deleted" or existing.retain_until <= self.store.clock():
                    raise MuthError(
                        409, "identity_enrollment_closed", "Esta inscrição já terminou."
                    )
                previous = self._payload(existing)
                if previous.model_dump(exclude={"embedding"}) != payload.model_dump(
                    exclude={"embedding"}
                ) or any(
                    abs(a - b) > 1e-12
                    for a, b in zip(previous.embedding, payload.embedding, strict=True)
                ):
                    raise MuthError(
                        409, "identity_enrollment_conflict", "A sessão já tem outra inscrição."
                    )
                return self._view(existing)
            serialised = payload.model_dump_json()
            active = db.scalar(
                select(func.count())
                .select_from(IdentityRow)
                .where(
                    IdentityRow.tenant_id == principal.tenant_id,
                    IdentityRow.status == "provisional",
                    IdentityRow.retain_until > self.store.clock(),
                )
            )
            if active >= self.store.settings.identity_max_profiles:
                raise MuthError(
                    429, "identity_profile_limit", "O limite de identidades guardadas foi atingido."
                )
            if len(serialised.encode()) > 64 * 1024:
                raise MuthError(
                    422, "identity_payload_too_large", "Dados da identidade demasiado extensos."
                )
            now = self.store.clock()
            row = IdentityRow(
                identity_id=f"mth_idn_{uuid4().hex}",
                tenant_id=principal.tenant_id,
                source_session_id=session_id,
                status="provisional",
                model_fingerprint=model_fingerprint,
                dimension=payload.dimension,
                payload=self.store._encrypt(serialised),
                created_at=now,
                retain_until=now + self.store.settings.identity_retention_days * 86400,
            )
            db.add(row)
            self.store._event(db, principal, session_id, "identity_enrolled", request_id)
            db.flush()
            return self._view(row)

    def get(self, principal: Principal, identity_id: str) -> IdentityView:
        with self.store.db.transaction() as db:
            return self._view(self._row(db, principal, identity_id))

    def template(self, principal: Principal, identity_id: str) -> tuple[list[float], str]:
        """Trusted engine integration only; never expose the returned vector over HTTP."""
        with self.store.db.transaction() as db:
            payload = self._payload(self._row(db, principal, identity_id))
            return payload.embedding, payload.model_fingerprint

    def delete(self, principal: Principal, identity_id: str, request_id: str):
        with self.store.db.transaction() as db:
            row = self._row(db, principal, identity_id)
            changed = db.execute(
                update(IdentityRow)
                .where(self._visible(principal, identity_id))
                .values(status="deleted", payload=None)
            ).rowcount
            if changed != 1:
                raise MuthError(404, "identity_not_found", "Identidade não encontrada.")
            db.execute(
                update(SessionRow)
                .where(
                    SessionRow.session_id == row.source_session_id,
                    SessionRow.tenant_id == principal.tenant_id,
                )
                .values(enrollment_consent=None)
            )
            self.store._event(db, principal, row.source_session_id, "identity_deleted", request_id)

    def purge(self) -> int:
        with self.store.db.transaction() as db:
            now = self.store.clock()
            expired = list(
                db.scalars(
                    select(IdentityRow).where(
                        IdentityRow.status != "deleted", IdentityRow.retain_until <= now
                    )
                )
            )
            count = 0
            for row in expired:
                changed = db.execute(
                    update(IdentityRow)
                    .where(
                        IdentityRow.identity_id == row.identity_id,
                        IdentityRow.status != "deleted",
                        IdentityRow.retain_until <= now,
                    )
                    .values(status="deleted", payload=None)
                ).rowcount
                if not changed:
                    continue
                db.execute(
                    update(SessionRow)
                    .where(
                        SessionRow.session_id == row.source_session_id,
                        SessionRow.tenant_id == row.tenant_id,
                    )
                    .values(enrollment_consent=None)
                )
                actor = Principal(row.tenant_id, "identity-retention", frozenset())
                self.store._event(
                    db, actor, row.source_session_id, "identity_retention_purged", "retention"
                )
                count += 1
            return count
