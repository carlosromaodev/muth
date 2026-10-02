"""Atomic storage of a standard verification and its encrypted capture report."""

from sqlalchemy import and_, func, or_, select, text, update

from muth.capture_models import CaptureVerification
from muth.domain import Checks, Verification
from muth.errors import MuthError
from muth.security import Principal
from muth.storage import SessionRow


class CaptureStore:
    def __init__(self, store):
        self.store = store

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
                    self.store._event(
                        db, actor, session_id, "capture_retention_purged", "retention"
                    )
                    count += 1
            return count

    def create(self, principal, payload, request_id):
        settings, now = self.store.settings, self.store.clock()
        with self.store.db.transaction() as db:
            if not self.store.db.in_memory:
                db.execute(text("BEGIN IMMEDIATE"))
            self.purge()
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
                .values(retain_until=now + settings.capture_retention_days * 86400)
            )
            return session

    def result(self, principal, session_id) -> CaptureVerification:
        with self.store.db.transaction() as db:
            row = self.store._row(db, principal, session_id)
            if not row.capture_result:
                raise MuthError(409, "capture_pending", "A análise ainda não foi concluída.")
            return CaptureVerification.model_validate_json(self.store._decrypt(row.capture_result))

    def complete(self, principal, session_id, attempt, report, request_id):
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
            db.execute(
                update(SessionRow)
                .where(self.store._visible(principal, session_id))
                .values(capture_result=self.store._encrypt(report.model_dump_json()))
            )
