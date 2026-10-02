"""Atomic storage of a standard verification and its encrypted capture report."""

from sqlalchemy import and_, func, or_, select, text, update

from muth.capture_models import CaptureVerification
from muth.domain import Checks, Verification
from muth.errors import MuthError
from muth.security import Principal
from muth.storage import SessionRow


class CaptureStore:
    def __init__(self, store, learning=None, identities=None):
        self.store = store
        self.learning = learning
        self.identities = identities

    def purge(self):
        now, settings = self.store.clock(), self.store.settings
        expired = and_(
            SessionRow.tenant_id == settings.capture_tenant_id,
            SessionRow.status != "deleted",
            or_(
                SessionRow.retain_until <= now,
                and_(SessionRow.status == "awaiting_capture", SessionRow.expires_at <= now),
                and_(
                    SessionRow.status == "processing",
                    SessionRow.expires_at <= now,
                    SessionRow.claim_until <= now,
                ),
            ),
        )
        actor = Principal(settings.capture_tenant_id, "capture-retention", frozenset())
        with self.store.db.transaction() as db:
            ids = list(db.scalars(select(SessionRow.session_id).where(expired)))
            count = 0
            for session_id in ids:
                changed = db.execute(
                    update(SessionRow)
                    .where(expired, SessionRow.session_id == session_id)
                    .values(**self.store._scrub())
                ).rowcount
                if changed:
                    from muth.capture_learning import erase_capture_candidates

                    erase_capture_candidates(db, [session_id])
                    self.store._event(
                        db, actor, session_id, "capture_retention_purged", "retention"
                    )
                    count += 1
            return count

    def create(
        self,
        principal,
        payload,
        request_id,
        *,
        learning_opt_in=False,
        identity_enrollment_opt_in=False,
    ):
        settings, now = self.store.settings, self.store.clock()
        with self.store.db.transaction() as db:
            if not self.store.db.in_memory:
                db.execute(text("BEGIN IMMEDIATE"))
            self.purge()
            if self.learning:
                self.learning.purge()
            active = db.scalar(
                select(func.count())
                .select_from(SessionRow)
                .where(
                    SessionRow.tenant_id == settings.capture_tenant_id,
                    SessionRow.status != "deleted",
                    SessionRow.retain_until > now,
                )
            )
            if active >= settings.capture_max_sessions:
                raise MuthError(
                    429, "capture_session_limit", "A captura está ocupada. Tente mais tarde."
                )
            session = self.store.create(principal, payload, request_id)
            db.execute(
                update(SessionRow)
                .where(SessionRow.session_id == session.session_id)
                .values(
                    retain_until=now
                    + (
                        settings.capture_learning_retention_days
                        if learning_opt_in
                        else settings.capture_retention_days
                    )
                    * 86400
                )
            )
            if learning_opt_in:
                self.learning.create(principal, session.session_id, request_id)
            if identity_enrollment_opt_in:
                from muth.identity_models import EnrollmentConsent

                consent = EnrollmentConsent(
                    accepted=True, policy_version=settings.identity_enrollment_policy_version
                )
                db.execute(
                    update(SessionRow)
                    .where(SessionRow.session_id == session.session_id)
                    .values(enrollment_consent=self.store._encrypt(consent.model_dump_json()))
                )
            return self.store.get(principal, session.session_id)

    def enrollment_requested(self, principal, session_id):
        with self.store.db.transaction() as db:
            return bool(self.store._row(db, principal, session_id).enrollment_consent)

    def result(self, principal, session_id) -> CaptureVerification:
        with self.store.db.transaction() as db:
            row = self.store._row(db, principal, session_id)
            if not row.capture_result:
                raise MuthError(409, "capture_pending", "A análise ainda não foi concluída.")
            report = CaptureVerification.model_validate_json(
                self.store._decrypt(row.capture_result)
            )
            if self.learning:
                report.learning = self.learning.info(principal, session_id)
            return report

    def complete(
        self, principal, session_id, attempt, report, request_id, enrollment_material=None
    ):
        standard = Verification(
            verification_id=report.verification_id,
            created_at=report.created_at,
            status=report.status,
            mode=report.mode,
            policy_version=report.scoring_version,
            reasons=report.limitations,
            checks=Checks(
                document=report.checks.document_front,
                liveness=report.checks.liveness,
                face_match=report.checks.face_match,
            ),
        )
        with self.store.db.transaction() as db:
            self.store.complete(principal, session_id, attempt, standard, request_id)
            if self.learning:
                report.learning = self.learning.collect(principal, session_id, report, request_id)
            db.execute(
                update(SessionRow)
                .where(self.store._visible(principal, session_id))
                .values(capture_result=self.store._encrypt(report.model_dump_json()))
            )
            if enrollment_material is not None:
                from muth.identity_service import IdentityRegistration

                db.flush()
                try:
                    identity = self.identities.enroll(
                        principal,
                        session_id,
                        report.document_data,
                        *enrollment_material,
                        request_id,
                    )
                except MuthError:
                    report.identity = IdentityRegistration(
                        status="unavailable",
                        explanation=(
                            "O registo não foi guardado; "
                            "o resultado da verificação continua disponível."
                        ),
                    )
                else:
                    report.identity = IdentityRegistration(
                        status="enrolled_provisional",
                        identity_id=identity.identity_id,
                        biometric_template_saved=True,
                        retain_until=identity.retain_until,
                        explanation="Template e dados documentais cifrados; identidade provisória.",
                    )
                db.execute(
                    update(SessionRow)
                    .where(self.store._visible(principal, session_id))
                    .values(capture_result=self.store._encrypt(report.model_dump_json()))
                )
