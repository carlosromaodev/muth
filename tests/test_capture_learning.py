import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from pydantic import BaseModel, SecretStr, ValidationError
from sqlalchemy import select

from muth.capture_learning import (
    CaptureDocumentCorrections,
    CaptureLearningReview,
    CaptureLearningRow,
    CaptureLearningService,
    erase_capture_candidates,
)
from muth.domain import Check, Checks, CreateSession, DocumentCheck, Verification
from muth.engines.biometric import Calibration
from muth.errors import MuthError
from muth.learning import LearningService, cohort, subject_hash
from muth.security import Principal
from muth.storage import (
    ActiveCalibrationRow,
    CalibrationMemberRow,
    CalibrationRow,
    Database,
    LearningSampleRow,
    SessionRow,
    SessionStore,
)
from tests.test_sessions import settings


class ExtractedField(BaseModel):
    value: str
    confidence: float = 0.9
    source_side: str = "front"
    validation: str = "unvalidated"
    source: str = "ocr"


class CaptureLearningTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.config = settings().model_copy(update={"engine_mode": "biometric"})
        self.db = Database(self.config)
        self.addCleanup(self.db.engine.dispose)
        self.store = SessionStore(self.db, self.config, clock=lambda: self.clock[0])
        self.portal = Principal("capture-portal", "browser-capture", frozenset({"read", "verify"}))
        self.reviewer = Principal("operators", "reviewer-1", frozenset({"capture_review"}))
        self.engine = SimpleNamespace(fingerprint="a" * 64, calibration=Calibration(0.8))
        self.learning = LearningService(
            self.store, SimpleNamespace(face=self.engine, liveness=self.engine)
        )
        self.service = CaptureLearningService(self.store, self.learning)
        from datetime import UTC, datetime

        self.result = Verification(
            verification_id="mth_vfy_test_capture",
            created_at=datetime.fromtimestamp(self.clock[0], UTC),
            status="review",
            mode="biometric",
            policy_version="capture-v1",
            reasons=["uncalibrated"],
            checks=Checks(
                document=DocumentCheck(
                    outcome="inconclusive", reasons=[], provider="document", model_version="v1"
                ),
                face_match=Check(
                    outcome="inconclusive",
                    reasons=[],
                    provider="face",
                    model_version="v1",
                    score=0.9,
                    score_kind="cosine_similarity",
                    model_fingerprint=self.engine.fingerprint,
                ),
                liveness=Check(
                    outcome="inconclusive",
                    reasons=[],
                    provider="pad",
                    model_version="v1",
                    score=0.9,
                    score_kind="liveness_softmax",
                    model_fingerprint=self.engine.fingerprint,
                ),
            ),
        )
        self.report = SimpleNamespace(
            document_data=SimpleNamespace(
                fields={
                    "name": ExtractedField(value="Pessoa de teste"),
                    "document_number": ExtractedField(value="DOC-12345"),
                }
            )
        )

    def create(self, *, opt_in=True, complete=True):
        payload = CreateSession.model_validate(
            {
                "consent": {
                    "accepted": True,
                    "purpose": "onboarding",
                    "policy_version": "capture-privacy-v1",
                },
                "device_group": "web",
            }
        )
        sid = self.store.create(self.portal, payload, "create").session_id
        if opt_in:
            self.service.create(self.portal, sid, "opt-in")
        if complete:
            claim = self.store.claim(self.portal, sid, "attempt-test", "fingerprint", "claim")
            self.store.complete(self.portal, sid, claim.attempt, self.result, "complete")
            self.service.collect(self.portal, sid, self.report, "collect")
        return sid

    def review(self, sid, **changes):
        payload = {
            "independent_evidence_confirmed": True,
            "subject_reference": "registry:person-123",
            "source_reference": "independent-review-123",
            "face_label": "genuine",
            "liveness_label": "live",
            **changes,
        }
        return self.service.review(
            self.reviewer, sid, CaptureLearningReview.model_validate(payload), "review"
        )

    def samples(self):
        with self.db.transaction() as db:
            return list(db.scalars(select(LearningSampleRow)))

    def test_no_opt_in_is_not_retained_or_collected(self):
        sid = self.create(opt_in=False)
        self.assertEqual(self.service.info(self.portal, sid).status, "not_opted_in")
        self.assertEqual(self.service.queue(self.reviewer), [])
        self.assertEqual(self.samples(), [])
        with self.db.transaction() as db:
            self.assertEqual(list(db.scalars(select(CaptureLearningRow))), [])

    def test_opt_in_freezes_encrypted_candidates_without_training(self):
        sid = self.create()
        info = self.service.info(self.portal, sid)
        self.assertEqual(info.status, "pending_review")
        self.assertTrue(info.consented)
        self.assertEqual(info.samples, 0)
        self.assertEqual(self.samples(), [])
        self.assertEqual(self.learning.run("capture-portal")["face"]["status"], "collecting")
        with self.db.transaction() as db:
            row = db.get(CaptureLearningRow, sid)
            self.assertNotIn("Pessoa", row.payload)
            self.assertNotIn("DOC-12345", row.payload)
            candidate = json.loads(self.store._decrypt(row.payload))
            self.assertEqual(candidate["checks"]["face"]["model_fingerprint"], "a" * 64)
            self.assertEqual(candidate["document_fields"]["name"]["value"], "Pessoa de teste")
            session = CreateSession.model_validate_json(
                self.store._decrypt(db.get(SessionRow, sid).payload)
            )
            self.assertFalse(session.consent.learning_opt_in)
            self.assertIsNone(session.subject_reference)
            self.assertNotIn("image", json.dumps(candidate))
            self.assertNotIn("embedding", json.dumps(candidate))
        self.assertEqual(len(self.service.queue(self.reviewer)), 1)
        self.service.collect(self.portal, sid, self.report, "replay")
        self.assertEqual(len(self.service.queue(self.reviewer)), 1)

    def test_proposals_are_untrusted_encrypted_and_bounded(self):
        sid = self.create()
        self.service.propose(
            self.portal,
            sid,
            CaptureDocumentCorrections(fields={"name": " Nome corrigido "}),
            "proposal",
        )
        candidate = self.service.queue(self.reviewer)[0]
        self.assertEqual(candidate["proposed_fields"], {"name": "Nome corrigido"})
        self.assertIsNone(candidate["review"])
        self.assertEqual(self.samples(), [])
        for value in (
            {"fields": {"face_label": "genuine"}},
            {"fields": {"name": "a" * 257}},
            {"fields": {"name": "name\x00"}},
            {"fields": {"birth_date": "2025-02-30"}},
            {"fields": {"birth_date": "2025-1-2"}},
            {"fields": {"name": ""}},
            {"fields": {"name": "ok"}, "subject_reference": "registry:spoof"},
            {"fields": {}},
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                CaptureDocumentCorrections.model_validate(value)

    def test_review_requires_scope_and_independent_stable_identity(self):
        sid = self.create()
        review = CaptureLearningReview(
            independent_evidence_confirmed=True,
            subject_reference="registry:person-123",
            source_reference="independent-review-123",
            face_label="genuine",
        )
        with self.assertRaises(MuthError) as failure:
            self.service.review(self.portal, sid, review, "browser-self-label")
        self.assertEqual(failure.exception.status, 403)
        with self.assertRaises(MuthError):
            self.service.queue(self.portal)
        with self.assertRaises(MuthError):
            self.service.queue(self.reviewer, 101)
        for change in (
            {"subject_reference": "123456789"},
            {"independent_evidence_confirmed": False},
            {"independent_evidence_confirmed": "true"},
            {"source_reference": "tiny"},
            {"face_label": "impostor"},
            {"face_label": "impostor", "capture_subject_reference": "registry:person-123"},
            {"liveness_label": "spoof"},
            {"liveness_label": "live", "attack_type": "print"},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                CaptureLearningReview.model_validate(review.model_dump() | change)

    def test_review_labels_use_existing_calibration_contract_and_are_immutable(self):
        sid = self.create()
        info = self.review(sid, document_fields={"name": "Pessoa confirmada"})
        self.assertEqual(info.status, "reviewed")
        self.assertEqual(info.samples, 2)
        rows = self.samples()
        self.assertEqual(len(rows), 2)
        expected_subject = subject_hash(self.store, "capture-portal", "registry:person-123")
        self.assertTrue(all(row.subject_hash == expected_subject for row in rows))
        self.assertTrue(all(row.split == cohort(expected_subject) for row in rows))
        self.assertTrue(all(row.state == "labelled" for row in rows))
        self.assertEqual(self.service.queue(self.reviewer), [])
        self.review(sid, document_fields={"name": "Pessoa confirmada"})
        self.assertEqual(len(self.samples()), 2)
        with self.assertRaises(MuthError) as failure:
            self.review(sid, source_reference="another-review-123")
        self.assertEqual(failure.exception.code, "review_conflict")
        with self.assertRaises(MuthError):
            self.service.propose(
                self.portal, sid, CaptureDocumentCorrections(fields={"name": "later"}), "late"
            )

    def test_impostor_and_pad_use_captured_person_and_exclude_cross_split(self):
        sid = self.create()
        claimed = "registry:person-123"
        claimed_split = cohort(subject_hash(self.store, "capture-portal", claimed))
        captured = next(
            f"registry:other-{i}"
            for i in range(100)
            if cohort(subject_hash(self.store, "capture-portal", f"registry:other-{i}"))
            != claimed_split
        )
        self.review(sid, face_label="impostor", capture_subject_reference=captured)
        for row in self.samples():
            self.assertEqual(row.state, "excluded")
            payload = json.loads(self.store._decrypt(row.payload))
            if row.role == "liveness":
                self.assertEqual(
                    payload["pair"], subject_hash(self.store, "capture-portal", captured)
                )
            else:
                self.assertEqual(len(payload["pair"].split(":")), 2)

    def test_injection_and_disagreement_stay_excluded(self):
        self.result.checks.liveness.reasons = ["liveness_ensemble_disagreement"]
        sid = self.create()
        self.review(sid, liveness_label="spoof", attack_type="injection")
        pad = next(row for row in self.samples() if row.role == "liveness")
        self.assertEqual(pad.state, "excluded")

    def test_withdrawal_erases_payload_samples_and_revokes_affected_policy(self):
        sid = self.create()
        self.review(sid)
        sample = self.samples()[0]
        with self.db.transaction() as db:
            db.add(
                CalibrationRow(
                    policy_id="mth_cal_capture_test",
                    tenant_id="capture-portal",
                    role="face",
                    model_fingerprint=self.engine.fingerprint,
                    state="active",
                    threshold=0.8,
                    snapshot_hash="s" * 64,
                    test_panel_hash="t" * 64,
                    report=self.store._encrypt("{}"),
                    created_at=self.clock[0],
                )
            )
            db.add(
                ActiveCalibrationRow(
                    tenant_id="capture-portal",
                    role="face",
                    model_fingerprint=self.engine.fingerprint,
                    policy_id="mth_cal_capture_test",
                    generation=1,
                )
            )
            db.add(
                CalibrationMemberRow(policy_id="mth_cal_capture_test", sample_id=sample.sample_id)
            )
        info = self.service.withdraw(self.portal, sid, "withdraw")
        self.assertEqual(info.status, "withdrawn")
        self.assertFalse(info.consented)
        self.assertEqual(self.samples(), [])
        with self.db.transaction() as db:
            row = db.get(CaptureLearningRow, sid)
            self.assertIsNone(row.payload)
            self.assertEqual(row.state, "withdrawn")
            self.assertEqual(row.retain_until, self.clock[0] + 86400)
            self.assertEqual(db.get(CalibrationRow, "mth_cal_capture_test").state, "revoked")
            active = db.get(
                ActiveCalibrationRow, ("capture-portal", "face", self.engine.fingerprint)
            )
            self.assertIsNone(active.policy_id)
            self.assertEqual(active.generation, 2)
            session = CreateSession.model_validate_json(
                self.store._decrypt(db.get(SessionRow, sid).payload)
            )
            self.assertFalse(session.consent.learning_opt_in)
            self.assertIsNone(session.subject_reference)
            self.assertEqual(db.get(SessionRow, sid).retain_until, self.clock[0] + 86400)
        self.assertEqual(self.service.withdraw(self.portal, sid, "withdraw-again"), info)
        with self.assertRaises(MuthError):
            self.review(sid)

    def test_expiry_and_deletion_helper_erase_candidates_and_samples(self):
        sid = self.create()
        self.review(sid)
        with self.db.transaction() as db:
            db.get(CaptureLearningRow, sid).retain_until = self.clock[0]
        self.assertEqual(self.service.purge(), 1)
        self.assertEqual(self.samples(), [])
        another = self.create()
        self.review(another)
        with self.db.transaction() as db:
            erase_capture_candidates(db, [another])
        self.assertEqual(self.samples(), [])
        with self.db.transaction() as db:
            self.assertEqual(list(db.scalars(select(CaptureLearningRow))), [])

    def test_tenant_isolation_and_late_opt_in(self):
        sid = self.create()
        other = Principal("alpha", "other", frozenset({"capture_review"}))
        with self.assertRaises(MuthError) as failure:
            self.service.info(other, sid)
        self.assertEqual(failure.exception.status, 404)
        unconsented = self.create(opt_in=False)
        with self.assertRaises(MuthError) as failure:
            self.service.create(self.portal, unconsented, "late")
        self.assertEqual(failure.exception.code, "consent_too_late")

    def test_review_failure_rolls_back_labels_subject_and_candidate(self):
        sid = self.create()
        original_feedback = self.learning.feedback
        calls = [0]

        def fail_second(*args):
            calls[0] += 1
            if calls[0] == 2:
                raise MuthError(409, "injected_failure", "Injected test failure.")
            return original_feedback(*args)

        with patch.object(self.learning, "feedback", side_effect=fail_second):
            with self.assertRaises(MuthError):
                self.review(sid)
        self.assertEqual(self.samples(), [])
        self.assertEqual(self.service.info(self.portal, sid).status, "pending_review")
        with self.db.transaction() as db:
            payload = CreateSession.model_validate_json(
                self.store._decrypt(db.get(SessionRow, sid).payload)
            )
            self.assertIsNone(payload.subject_reference)
            self.assertFalse(payload.consent.learning_opt_in)

    def test_demo_or_unavailable_measurements_never_produce_training_samples(self):
        self.store.settings = self.config.model_copy(update={"engine_mode": "demo"})
        sid = self.create()
        self.assertEqual(self.review(sid).samples, 0)
        self.assertEqual(self.samples(), [])
        self.store.settings = self.config
        self.result.checks.face_match.score = None
        self.result.checks.liveness.model_fingerprint = None
        sid = self.create()
        self.assertEqual(self.review(sid).samples, 0)
        self.assertEqual(self.samples(), [])

    def test_document_only_review_does_not_create_unlabelled_biometrics(self):
        sid = self.create()
        info = self.review(
            sid,
            face_label=None,
            liveness_label=None,
            document_fields={"name": "Pessoa confirmada"},
        )
        self.assertEqual(info.status, "reviewed")
        self.assertEqual(info.samples, 0)
        self.assertEqual(self.samples(), [])

    def test_malformed_model_fingerprint_and_negative_pad_cannot_train(self):
        self.result.checks.face_match.model_fingerprint = "unverified-model"
        self.result.checks.liveness.score = -0.5
        sid = self.create()
        self.assertEqual(self.review(sid).samples, 0)
        self.assertEqual(self.samples(), [])

    def test_learning_configuration_blocks_new_consent_but_preserves_withdrawal(self):
        sid = self.create()
        for changes in (
            {"learning_enabled": False},
            {"capture_learning_enabled": False},
        ):
            self.store.settings = self.config.model_copy(update=changes)
            other = self.create(opt_in=False, complete=False)
            with self.assertRaises(MuthError) as failure:
                self.service.create(self.portal, other, "disabled")
            self.assertEqual(failure.exception.code, "learning_disabled")
        self.assertEqual(
            self.service.withdraw(self.portal, sid, "withdraw-disabled").status, "withdrawn"
        )

    def test_persistent_review_transaction_and_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = settings(
                database_url=f"sqlite:///{Path(directory) / 'capture.sqlite'}",
                data_key=SecretStr(Fernet.generate_key().decode()),
            ).model_copy(update={"engine_mode": "biometric"})
            db = Database(config)
            try:
                db.migrate()
                store = SessionStore(db, config, clock=lambda: self.clock[0])
                learning = LearningService(store)
                service = CaptureLearningService(store, learning)
                old = self.db, self.store, self.learning, self.service
                self.db, self.store, self.learning, self.service = db, store, learning, service
                try:
                    sid = self.create()
                    self.assertEqual(self.review(sid).samples, 2)
                    second = self.create()
                    original_feedback = learning.feedback
                    calls = [0]

                    def fail_second(*args):
                        calls[0] += 1
                        if calls[0] == 2:
                            raise MuthError(409, "injected_failure", "Injected test failure.")
                        return original_feedback(*args)

                    with patch.object(learning, "feedback", side_effect=fail_second):
                        with self.assertRaises(MuthError):
                            self.review(second)
                    self.assertEqual(
                        self.service.info(self.portal, second).status, "pending_review"
                    )
                    self.assertEqual(len(self.samples()), 2)
                finally:
                    self.db, self.store, self.learning, self.service = old
                db.migrate("0003_capture", downgrade=True)
                db.migrate()
            finally:
                db.engine.dispose()
