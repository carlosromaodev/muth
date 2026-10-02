import json
import math
import random
import struct
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError
from sqlalchemy import inspect, select

from muth.capture_models import CaptureChecks, CaptureScore, CaptureVerification
from muth.document_models import DocumentData, DocumentField
from muth.domain import Check, Checks, CreateSession, DocumentCheck, Verification
from muth.errors import MuthError
from muth.identity_models import EnrollmentConsent
from muth.identity_store import IdentityRow, IdentityStore, erase_session_identities
from muth.security import Principal
from muth.storage import Base, Database, SessionRow, SessionStore
from tests.test_sessions import settings


class IdentityStoreTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.config = settings().model_copy(
            update={
                "engine_mode": "biometric",
                "identity_enrollment_policy_version": "identity-enrollment-v1",
                "identity_retention_days": 365,
            }
        )
        self.db = Database(self.config)
        self.addCleanup(self.db.engine.dispose)
        self.store = SessionStore(self.db, self.config, clock=lambda: self.clock[0])
        self.identities = IdentityStore(self.store)
        self.portal = Principal("capture-portal", "browser-capture", frozenset({"read", "verify"}))
        self.other = Principal("other", "other-key", frozenset({"read", "verify", "delete"}))
        self.vector = [1.0] + [0.0] * 127
        self.fingerprint = "a" * 64
        self.document = DocumentData(
            status="partial",
            document_type="bi",
            issuing_country="AO",
            fields={
                "name": DocumentField(value="Pessoa de teste", source_side="front"),
                "document_number": DocumentField(value="AO-12345", source_side="front"),
            },
        )

    def create(self, *, opt_in=True, complete=True, mode="biometric", outcome="inconclusive"):
        created = self.store.create(
            self.portal,
            CreateSession.model_validate(
                {
                    "consent": {
                        "accepted": True,
                        "purpose": "onboarding",
                        "policy_version": "capture-privacy-v1",
                    },
                    "device_group": "web",
                }
            ),
            "create",
        )
        sid = created.session_id
        if opt_in:
            consent = EnrollmentConsent(accepted=True, policy_version="identity-enrollment-v1")
            with self.db.transaction() as db:
                db.get(SessionRow, sid).enrollment_consent = self.store._encrypt(
                    consent.model_dump_json()
                )
        if complete:
            face = Check(
                outcome=outcome,
                reasons=[],
                provider="sface",
                model_version="v1",
                model_fingerprint=self.fingerprint,
                score=0.9,
                score_kind="cosine_similarity",
            )
            pad = Check(
                outcome="inconclusive",
                reasons=[],
                provider="pad",
                model_version="v1",
                model_fingerprint="b" * 64,
                score=0.9,
                score_kind="liveness_softmax",
            )
            doc = DocumentCheck(
                outcome="inconclusive", reasons=[], provider="document", model_version="v1"
            )
            back = Check(
                outcome="inconclusive", reasons=[], provider="document", model_version="v1"
            )
            standard = Verification(
                verification_id="mth_vfy_fixture",
                created_at=datetime.fromtimestamp(self.clock[0], UTC),
                status="review",
                mode=mode,
                policy_version="capture-v1",
                reasons=["uncalibrated"],
                checks=Checks(face_match=face, liveness=pad, document=doc),
            )
            report = CaptureVerification(
                verification_id=standard.verification_id,
                created_at=standard.created_at,
                status="review",
                mode=mode,
                checks=CaptureChecks(
                    face_match=face,
                    liveness=pad,
                    document_front=doc,
                    document_back=back,
                ),
                score=CaptureScore(kind="indicative", value=5.5, explanation="Nota preliminar."),
                limitations=["document_authenticity_unavailable"],
                scoring_version="capture-v1",
                reasons=["uncalibrated"],
                document_data=self.document,
            )
            claim = self.store.claim(self.portal, sid, "attempt", "fingerprint", "claim")
            self.store.complete(self.portal, sid, claim.attempt, standard, "complete")
            with self.db.transaction() as db:
                db.get(SessionRow, sid).capture_result = self.store._encrypt(
                    report.model_dump_json()
                )
        return sid

    def enroll(self, sid, **changes):
        return self.identities.enroll(
            self.portal,
            sid,
            changes.get("document_data", self.document),
            changes.get("embedding", self.vector),
            changes.get("model_fingerprint", self.fingerprint),
            "enroll",
        )

    def assert_error(self, code, operation, *args):
        with self.assertRaises(MuthError) as raised:
            operation(*args)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_enrollment_encrypted_tenant_profile_never_confirms_authenticity(self):
        sid = self.create()
        identity = self.enroll(sid)
        self.assertRegex(identity.identity_id, r"^mth_idn_[a-f0-9]{32}$")
        self.assertEqual(identity.status, "provisional")
        self.assertFalse(identity.authenticity_confirmed)
        self.assertFalse(identity.document_data.authenticity_confirmed)
        self.assertTrue(identity.biometric_template_saved)
        self.assertEqual(identity.document_data, self.document)
        self.assertEqual(identity.retain_until.timestamp(), self.clock[0] + 365 * 86400)
        public = identity.model_dump_json()
        for private in ("embedding", "model_fingerprint", "subject_hash"):
            self.assertNotIn(private, public)
        with self.db.transaction() as db:
            row = db.get(IdentityRow, identity.identity_id)
            for private in ("Pessoa de teste", "AO-12345", "embedding", "consent"):
                self.assertNotIn(private, row.payload)
            payload = json.loads(self.store._decrypt(row.payload))
            self.assertEqual(payload["embedding"], self.vector)
            self.assertEqual(payload["document_data"], self.document.model_dump())
            self.assertEqual(payload["consent"]["purpose"], "identity_enrollment")
            self.assertEqual(row.dimension, 128)
            self.assertEqual(row.tenant_id, self.portal.tenant_id)
        self.assertEqual(
            self.identities.template(self.portal, identity.identity_id),
            (self.vector, self.fingerprint),
        )

    def test_replay_is_idempotent_and_only_enrollment_once_is_audited(self):
        sid = self.create()
        first = self.enroll(sid)
        self.assertEqual(self.enroll(sid), first)
        events = self.store.events(self.portal, sid)
        self.assertEqual(sum(event.event_type == "identity_enrolled" for event in events), 1)
        alternate = [0.0, 1.0] + [0.0] * 126
        self.assert_error(
            "identity_enrollment_conflict", lambda: self.enroll(sid, embedding=alternate)
        )

    def test_realistic_float32_embedding_replay_survives_storage_normalisation(self):
        generator = random.Random(12)
        values = [
            struct.unpack("f", struct.pack("f", generator.uniform(-1, 1)))[0] for _ in range(128)
        ]
        norm = math.sqrt(math.fsum(value * value for value in values))
        vector = [struct.unpack("f", struct.pack("f", value / norm))[0] for value in values]
        sid = self.create()
        first = self.enroll(sid, embedding=vector)
        self.assertEqual(self.enroll(sid, embedding=vector).identity_id, first.identity_id)
        changed = vector.copy()
        changed[0] += 0.00001
        self.assert_error(
            "identity_enrollment_conflict", lambda: self.enroll(sid, embedding=changed)
        )

    def test_same_session_concurrent_enrollment_is_idempotent(self):
        sid = self.create()
        with ThreadPoolExecutor(max_workers=2) as executor:
            identities = list(executor.map(self.enroll, [sid, sid]))
        self.assertEqual(identities[0].identity_id, identities[1].identity_id)
        with self.db.transaction() as db:
            self.assertEqual(len(list(db.scalars(select(IdentityRow)))), 1)

    def test_enrollment_requires_separate_explicit_current_policy_consent(self):
        sid = self.create(opt_in=False)
        self.assert_error("enrollment_consent_required", self.enroll, sid)
        for invalid in (False, "true", 1, None):
            with self.subTest(accepted=invalid), self.assertRaises(ValidationError):
                EnrollmentConsent(accepted=invalid, policy_version="identity-enrollment-v1")
        with self.db.transaction() as db:
            db.get(SessionRow, sid).enrollment_consent = self.store._encrypt(
                EnrollmentConsent(accepted=True, policy_version="old-policy").model_dump_json()
            )
        self.assert_error("enrollment_consent_invalid", self.enroll, sid)

    def test_capture_completion_biometric_mode_and_nonfailed_evidence_required(self):
        sid = self.create(complete=False)
        self.assert_error("capture_pending", self.enroll, sid)
        demo = self.create(mode="demo")
        self.assert_error("identity_unavailable", self.enroll, demo)
        failed = self.create(outcome="fail")
        self.assert_error("identity_evidence_failed", self.enroll, failed)
        self.config.engine_mode = "demo"
        self.assert_error("identity_unavailable", self.enroll, failed)

    def test_enrollment_disabled_by_operator_configuration(self):
        sid = self.create()
        self.config.identity_enrollment_enabled = False
        self.assert_error("identity_enrollment_disabled", self.enroll, sid)

    def test_vectors_and_fingerprints_validated_without_exposing_input(self):
        sid = self.create()
        invalid_vectors = [
            [],
            self.vector[:-1],
            self.vector + [0.0],
            [0.0] * 128,
            [float("nan")] + [0.0] * 127,
            [float("inf")] + [0.0] * 127,
            [True] + [0.0] * 127,
            ["1.0"] + [0.0] * 127,
            [2.0] + [0.0] * 127,
            [0.5] + [0.0] * 127,
        ]
        for vector in invalid_vectors:
            with self.subTest(vector=vector[:2]):
                error = self.assert_error(
                    "identity_template_invalid",
                    lambda vector=vector: self.enroll(sid, embedding=vector),
                )
                self.assertEqual(error.status, 422)
                self.assertEqual(error.message, "Template facial inválido.")
        for fingerprint in ("", "bad", "A" * 64, "a" * 63):
            with self.subTest(fingerprint=fingerprint):
                self.assert_error(
                    "identity_template_invalid",
                    lambda fingerprint=fingerprint: self.enroll(sid, model_fingerprint=fingerprint),
                )
        self.assert_error(
            "identity_model_conflict", lambda: self.enroll(sid, model_fingerprint="c" * 64)
        )

    def test_document_fields_are_bound_to_original_capture(self):
        sid = self.create()
        changed = self.document.model_copy(deep=True)
        changed.fields["name"].value = "Outra pessoa"
        self.assert_error(
            "identity_document_conflict", lambda: self.enroll(sid, document_data=changed)
        )
        absent = DocumentData(status="unavailable")
        self.assert_error("document_data_missing", lambda: self.enroll(sid, document_data=absent))

    def test_configured_runtime_mode_can_enroll_with_sufficient_biometric_signals(self):
        sid = self.create(mode="configured")
        self.assertEqual(self.enroll(sid).status, "provisional")

    def test_missing_unknown_and_low_biometric_measurements_cannot_enroll(self):
        for role, attribute, value in (
            ("face_match", "score", None),
            ("liveness", "score", None),
            ("face_match", "score_kind", "unknown"),
            ("liveness", "score_kind", "unknown"),
            ("face_match", "score", 0.362),
            ("liveness", "score", 0.799),
        ):
            with self.subTest(role=role, attribute=attribute, value=value):
                sid = self.create()
                with self.db.transaction() as db:
                    row = db.get(SessionRow, sid)
                    result = Verification.model_validate_json(self.store._decrypt(row.result))
                    setattr(getattr(result.checks, role), attribute, value)
                    row.result = self.store._encrypt(result.model_dump_json())
                self.assert_error("identity_evidence_insufficient", self.enroll, sid)

    def test_invalid_consent_ciphertext_is_rejected_with_sanitised_message(self):
        sid = self.create()
        with self.db.transaction() as db:
            db.get(SessionRow, sid).enrollment_consent = "invalid-private-consent"
        error = self.assert_error("enrollment_consent_invalid", self.enroll, sid)
        self.assertNotIn("invalid-private-consent", error.message)

    def test_all_identity_operations_are_tenant_scoped(self):
        sid = self.create()
        identity = self.enroll(sid)
        for operation in (
            lambda: self.identities.get(self.other, identity.identity_id),
            lambda: self.identities.template(self.other, identity.identity_id),
            lambda: self.identities.delete(self.other, identity.identity_id, "delete"),
        ):
            self.assertEqual(self.assert_error("identity_not_found", operation).status, 404)
        self.assert_error(
            "session_not_found",
            lambda: self.identities.enroll(
                self.other, sid, self.document, self.vector, self.fingerprint, "enroll"
            ),
        )
        self.assertEqual(self.identities.get(self.portal, identity.identity_id), identity)

    def test_deletion_scrubs_template_document_and_withdraws_consent(self):
        sid = self.create()
        identity = self.enroll(sid)
        self.identities.delete(self.portal, identity.identity_id, "delete")
        self.assert_error(
            "identity_not_found", self.identities.get, self.portal, identity.identity_id
        )
        self.assert_error("enrollment_consent_required", self.enroll, sid)
        with self.db.transaction() as db:
            row = db.get(IdentityRow, identity.identity_id)
            self.assertEqual(row.status, "deleted")
            self.assertIsNone(row.payload)
            self.assertIsNone(db.get(SessionRow, sid).enrollment_consent)
        events = self.store.events(self.portal, sid)
        self.assertEqual(sum(event.event_type == "identity_deleted" for event in events), 1)

    def test_expired_profiles_hidden_before_purge_and_scrubbed_afterwards(self):
        sid = self.create()
        identity = self.enroll(sid)
        self.clock[0] = identity.retain_until.timestamp()
        self.assert_error(
            "identity_not_found", self.identities.template, self.portal, identity.identity_id
        )
        self.assertEqual(self.identities.purge(), 1)
        self.assertEqual(self.identities.purge(), 0)
        with self.db.transaction() as db:
            self.assertIsNone(db.get(IdentityRow, identity.identity_id).payload)
            self.assertIsNone(db.get(SessionRow, sid).enrollment_consent)

    def test_identity_retention_is_independent_of_capture_retention(self):
        sid = self.create()
        identity = self.enroll(sid)
        self.clock[0] += 2 * 86400
        with self.db.transaction() as db:
            row = db.get(SessionRow, sid)
            row.retain_until = self.clock[0] - 1
            row.status = "deleted"
            row.payload = row.result = row.capture_result = None
        self.assertEqual(self.identities.get(self.portal, identity.identity_id), identity)
        self.assertEqual(self.identities.purge(), 0)

    def test_explicit_source_session_deletion_helper_erases_linked_profiles(self):
        sid = self.create()
        identity = self.enroll(sid)
        with self.db.transaction() as db:
            self.assertEqual(erase_session_identities(db, [sid]), 1)
            self.assertEqual(erase_session_identities(db, []), 0)
        self.assert_error(
            "identity_not_found", self.identities.get, self.portal, identity.identity_id
        )
        with self.db.transaction() as db:
            self.assertIsNone(db.get(IdentityRow, identity.identity_id).payload)
            self.assertIsNone(db.get(SessionRow, sid).enrollment_consent)

    def test_profile_and_audit_roll_back_with_outer_transaction(self):
        sid = self.create()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.db.transaction():
                self.enroll(sid)
                raise RuntimeError("rollback")
        with self.db.transaction() as db:
            self.assertEqual(list(db.scalars(select(IdentityRow))), [])
        events = self.store.events(self.portal, sid)
        self.assertFalse(any(event.event_type == "identity_enrolled" for event in events))

    def test_metadata_corruption_prevents_template_use(self):
        sid = self.create()
        identity = self.enroll(sid)
        with self.db.transaction() as db:
            db.get(IdentityRow, identity.identity_id).dimension = 512
        self.assert_error(
            "identity_template_invalid", self.identities.template, self.portal, identity.identity_id
        )


class IdentityMigrationTests(unittest.TestCase):
    def test_roundtrip_and_metadata_match(self):
        with tempfile.TemporaryDirectory() as folder:
            config = settings(
                database_url=f"sqlite:///{Path(folder) / 'identity.sqlite3'}",
                data_key=SecretStr(Fernet.generate_key().decode()),
            )
            db = Database(config)
            try:
                db.migrate()
                self.assertTrue(db.ready())
                with db.engine.connect() as connection:
                    self.assertEqual(
                        compare_metadata(MigrationContext.configure(connection), Base.metadata), []
                    )
                db.migrate("0004_capture_learning", downgrade=True)
                inspector = inspect(db.engine)
                self.assertNotIn("identity_profiles", inspector.get_table_names())
                self.assertNotIn(
                    "enrollment_consent",
                    {column["name"] for column in inspector.get_columns("verification_sessions")},
                )
                self.assertFalse(db.ready())
                db.migrate()
                self.assertTrue(db.ready())
                with db.engine.connect() as connection:
                    self.assertEqual(
                        compare_metadata(MigrationContext.configure(connection), Base.metadata), []
                    )
            finally:
                db.engine.dispose()
