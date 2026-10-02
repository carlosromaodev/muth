import hashlib
import hmac
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import (
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    create_engine,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from muth.config import Settings
from muth.domain import (
    AuditEvent,
    CreateSession,
    ReviewDecision,
    SessionStatus,
    SessionView,
    Verification,
    VerificationStatus,
)
from muth.errors import MuthError
from muth.media import ImageInput
from muth.security import Principal

SCHEMA_VERSION = "0005_identities"


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "verification_sessions"

    session_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float)
    retain_until: Mapped[float] = mapped_column(Float, index=True)
    payload: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    capture_result: Mapped[str | None] = mapped_column(Text)
    enrollment_consent: Mapped[str | None] = mapped_column(Text)
    idempotency_hash: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    attempt: Mapped[str | None] = mapped_column(String(32))
    claim_until: Mapped[float | None] = mapped_column(Float)


class AuditRow(Base):
    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    session_id: Mapped[str] = mapped_column(String(48), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[float] = mapped_column(Float)
    request_id: Mapped[str] = mapped_column(String(32))


class LearningSampleRow(Base):
    __tablename__ = "learning_samples"
    __table_args__ = (UniqueConstraint("tenant_id", "session_id", "role"),)

    sample_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    session_id: Mapped[str] = mapped_column(String(48), index=True)
    role: Mapped[str] = mapped_column(String(16))
    model_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    subject_hash: Mapped[str] = mapped_column(String(64))
    split: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)
    retain_until: Mapped[float] = mapped_column(Float)


class CalibrationRow(Base):
    __tablename__ = "calibration_versions"

    policy_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    role: Mapped[str] = mapped_column(String(16))
    model_fingerprint: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    threshold: Mapped[float] = mapped_column(Float)
    snapshot_hash: Mapped[str] = mapped_column(String(64), index=True)
    test_panel_hash: Mapped[str] = mapped_column(String(64))
    report: Mapped[str] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float)


class ActiveCalibrationRow(Base):
    __tablename__ = "active_calibrations"

    tenant_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    model_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_id: Mapped[str | None] = mapped_column(String(48))
    generation: Mapped[int] = mapped_column(Integer, default=0)


class CalibrationMemberRow(Base):
    __tablename__ = "calibration_members"

    policy_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    sample_id: Mapped[str] = mapped_column(String(48), primary_key=True, index=True)


class Database:
    def __init__(self, settings: Settings):
        # Register the contribution table for CLI migrations and in-memory stores.
        from muth import (
            capture_learning,  # noqa: F401
            identity_store,  # noqa: F401
        )

        options = {"connect_args": {"check_same_thread": False, "timeout": 15}}
        if settings.in_memory:
            options["poolclass"] = StaticPool
        else:
            filename = make_url(settings.database_url).database
            if filename:
                Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(settings.database_url, **options)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.lock = threading.RLock()
        self.local = threading.local()
        if settings.in_memory:
            Base.metadata.create_all(self.engine)
        self.in_memory = settings.in_memory

    @contextmanager
    def transaction(self):
        # Serialize the in-memory connection; predicates also protect cross-process writes.
        with self.lock:
            active = getattr(self.local, "session", None)
            if active is not None:
                connection = active.connection()
                # sqlite3's legacy mode does not BEGIN on SELECT or SAVEPOINT.
                # Releasing an outermost savepoint would otherwise commit writes
                # that must still belong to the enclosing transaction.
                if not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql("BEGIN")
                with active.begin_nested():
                    yield active
                return
            with self.sessions.begin() as session:
                self.local.session = session
                try:
                    yield session
                finally:
                    del self.local.session

    def ready(self) -> bool:
        def check(connection):
            connection.execute(select(SessionRow.session_id).limit(1))
            if not self.in_memory:
                version = connection.execute(text("SELECT version_num FROM alembic_version"))
                return version.scalar() == SCHEMA_VERSION
            return True

        try:
            with self.lock:
                active = getattr(self.local, "session", None)
                if active is not None:
                    return check(active)
                with self.engine.connect() as connection:
                    return check(connection)
        except Exception:
            return False

    def migrate(self, target: str = "head", *, downgrade: bool = False) -> None:
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            if downgrade:
                command.downgrade(config, target)
            else:
                command.upgrade(config, target)


@dataclass(frozen=True)
class Claim:
    attempt: str | None = None
    replay: Verification | None = None


class SessionStore:
    def __init__(self, db: Database, settings: Settings, *, clock=time.time):
        self.db = db
        self.settings = settings
        self.clock = clock
        raw_key = settings.data_key.get_secret_value().encode() or Fernet.generate_key()
        self.box = Fernet(raw_key)
        # Separate purpose for the keyed capture fingerprint; never persist raw image hashes.
        self.fingerprint_key = hmac.digest(raw_key, b"muth:capture-fingerprint:v1", "sha256")

    def _encrypt(self, value: str) -> str:
        return self.box.encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        return self.box.decrypt(value.encode()).decode()

    def _visible(self, principal: Principal, session_id: str):
        return and_(
            SessionRow.session_id == session_id,
            SessionRow.tenant_id == principal.tenant_id,
            SessionRow.status != "deleted",
            SessionRow.retain_until > self.clock(),
        )

    def _event(self, db, principal, session_id, event_type, request_id):
        db.add(
            AuditRow(
                event_id=f"mth_evt_{uuid4().hex}",
                tenant_id=principal.tenant_id,
                session_id=session_id,
                event_type=event_type,
                actor=principal.key_id,
                created_at=self.clock(),
                request_id=request_id,
            )
        )

    def _row(self, db, principal, session_id):
        row = db.scalar(select(SessionRow).where(self._visible(principal, session_id)))
        if row is None:
            raise MuthError(404, "session_not_found", "Sessão não encontrada.")
        return row

    def _view(self, row: SessionRow) -> SessionView:
        payload = CreateSession.model_validate_json(self._decrypt(row.payload))
        status = row.status
        if status == "awaiting_capture" and row.expires_at <= self.clock():
            status = "expired"
        return SessionView(
            session_id=row.session_id,
            status=SessionStatus(status),
            created_at=datetime.fromtimestamp(row.created_at, UTC),
            expires_at=datetime.fromtimestamp(row.expires_at, UTC),
            retain_until=datetime.fromtimestamp(row.retain_until, UTC),
            consent=payload.consent,
            subject_reference=payload.subject_reference,
            device_group=payload.device_group,
            verification=Verification.model_validate_json(self._decrypt(row.result))
            if row.result
            else None,
        )

    def create(self, principal: Principal, payload: CreateSession, request_id: str) -> SessionView:
        now = self.clock()
        row = SessionRow(
            session_id=f"mth_ses_{uuid4().hex}",
            tenant_id=principal.tenant_id,
            status="awaiting_capture",
            created_at=now,
            expires_at=now + self.settings.session_ttl_seconds,
            retain_until=now + self.settings.retention_days * 86400,
            payload=self._encrypt(payload.model_dump_json()),
        )
        with self.db.transaction() as db:
            db.add(row)
            self._event(db, principal, row.session_id, "session_created", request_id)
        return self._view(row)

    def get(self, principal: Principal, session_id: str) -> SessionView:
        with self.db.transaction() as db:
            return self._view(self._row(db, principal, session_id))

    def fingerprint(self, principal, session_id, document: ImageInput, selfie: ImageInput) -> str:
        digest = hmac.new(self.fingerprint_key, digestmod="sha256")
        for part in (
            principal.tenant_id.encode(),
            session_id.encode(),
            document.content,
            selfie.content,
        ):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        return digest.hexdigest()

    def claim(self, principal, session_id, key, fingerprint, request_id) -> Claim:
        now = self.clock()
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        with self.db.transaction() as db:
            row = self._row(db, principal, session_id)
            same_key = row.idempotency_hash == key_hash
            if same_key and row.fingerprint != fingerprint:
                raise MuthError(409, "idempotency_conflict", "Chave já usada com outro payload.")
            if row.result:
                if same_key:
                    return Claim(replay=Verification.model_validate_json(self._decrypt(row.result)))
                raise MuthError(409, "session_completed", "Esta sessão já tem um resultado.")
            if row.expires_at <= now:
                raise MuthError(410, "session_expired", "O prazo de captura da sessão terminou.")
            attempt = uuid4().hex
            changed = db.execute(
                update(SessionRow)
                .where(
                    self._visible(principal, session_id),
                    or_(
                        SessionRow.status == "awaiting_capture",
                        and_(SessionRow.status == "processing", SessionRow.claim_until <= now),
                    ),
                )
                .values(
                    status="processing",
                    attempt=attempt,
                    claim_until=now + self.settings.processing_lease_seconds,
                    idempotency_hash=key_hash,
                    fingerprint=fingerprint,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if changed != 1:
                raise MuthError(409, "session_busy", "Existe uma tentativa em processamento.")
            self._event(db, principal, session_id, "verification_started", request_id)
            return Claim(attempt=attempt)

    def complete(self, principal, session_id, attempt, result: Verification, request_id):
        with self.db.transaction() as db:
            changed = db.execute(
                update(SessionRow)
                .where(
                    self._visible(principal, session_id),
                    SessionRow.status == "processing",
                    SessionRow.attempt == attempt,
                    SessionRow.claim_until > self.clock(),
                )
                .values(
                    status=result.status.value,
                    result=self._encrypt(result.model_dump_json()),
                    attempt=None,
                    claim_until=None,
                )
            ).rowcount
            if changed != 1:
                raise MuthError(409, "attempt_invalidated", "Tentativa expirada ou invalidada.")
            from muth.learning import collect_samples

            collect_samples(self, db, self._row(db, principal, session_id), result)
            self._event(db, principal, session_id, "verification_completed", request_id)

    def release(self, principal, session_id, attempt, request_id):
        with self.db.transaction() as db:
            changed = db.execute(
                update(SessionRow)
                .where(
                    self._visible(principal, session_id),
                    SessionRow.status == "processing",
                    SessionRow.attempt == attempt,
                )
                .values(status="awaiting_capture", attempt=None, claim_until=None)
            ).rowcount
            if changed:
                self._event(db, principal, session_id, "verification_failed", request_id)

    def review(self, principal, session_id, decision: ReviewDecision, request_id):
        with self.db.transaction() as db:
            row = self._row(db, principal, session_id)
            if row.status != "review" or not row.result:
                raise MuthError(409, "not_reviewable", "A sessão não está pendente de revisão.")
            result = Verification.model_validate_json(self._decrypt(row.result))
            result.status = VerificationStatus.REJECTED
            result.reasons.append(f"manual_review:{decision.reason_code}")
            changed = db.execute(
                update(SessionRow)
                .where(self._visible(principal, session_id), SessionRow.status == "review")
                .values(status="rejected", result=self._encrypt(result.model_dump_json()))
            ).rowcount
            if changed != 1:
                raise MuthError(409, "review_conflict", "A sessão mudou durante a revisão.")
            self._event(db, principal, session_id, "manual_rejection", request_id)
        return self.get(principal, session_id)

    @staticmethod
    def _scrub():
        return dict(
            status="deleted",
            payload=None,
            result=None,
            capture_result=None,
            enrollment_consent=None,
            idempotency_hash=None,
            fingerprint=None,
            attempt=None,
            claim_until=None,
        )

    def delete(self, principal, session_id, request_id):
        with self.db.transaction() as db:
            self._row(db, principal, session_id)
            changed = db.execute(
                update(SessionRow)
                .where(self._visible(principal, session_id))
                .values(**self._scrub())
            ).rowcount
            if changed != 1:
                raise MuthError(404, "session_not_found", "Sessão não encontrada.")
            from muth.capture_learning import erase_capture_candidates
            from muth.learning import erase_samples

            erase_samples(db, [session_id])
            erase_capture_candidates(db, [session_id])
            from muth.identity_store import erase_session_identities

            erase_session_identities(db, [session_id])
            self._event(db, principal, session_id, "session_deleted", request_id)

    def events(self, principal, session_id, limit=100) -> list[AuditEvent]:
        with self.db.transaction() as db:
            # Audit remains readable after deletion, but never across tenants.
            known = db.scalar(
                select(SessionRow.session_id).where(
                    SessionRow.session_id == session_id, SessionRow.tenant_id == principal.tenant_id
                )
            )
            if known is None:
                raise MuthError(404, "session_not_found", "Sessão não encontrada.")
            rows = db.scalars(
                select(AuditRow)
                .where(AuditRow.tenant_id == principal.tenant_id, AuditRow.session_id == session_id)
                .order_by(AuditRow.created_at, AuditRow.event_id)
                .limit(limit)
            )
            return [
                AuditEvent(
                    event_id=row.event_id,
                    session_id=row.session_id,
                    event_type=row.event_type,
                    actor=row.actor,
                    created_at=datetime.fromtimestamp(row.created_at, UTC),
                    request_id=row.request_id,
                )
                for row in rows
            ]

    def purge(self) -> int:
        with self.db.transaction() as db:
            rows = db.scalars(
                select(SessionRow).where(
                    SessionRow.retain_until <= self.clock(), SessionRow.status != "deleted"
                )
            ).all()
            count = 0
            for row in rows:
                changed = db.execute(
                    update(SessionRow)
                    .where(
                        SessionRow.session_id == row.session_id,
                        SessionRow.retain_until <= self.clock(),
                        SessionRow.status != "deleted",
                    )
                    .values(**self._scrub())
                ).rowcount
                if changed != 1:
                    continue
                from muth.capture_learning import erase_capture_candidates
                from muth.learning import erase_samples

                erase_samples(db, [row.session_id])
                erase_capture_candidates(db, [row.session_id])
                count += 1
                actor = Principal(row.tenant_id, "retention-job", frozenset())
                self._event(db, actor, row.session_id, "retention_purged", uuid4().hex)
            return count
