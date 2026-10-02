import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import select

from muth.config import SCOPES
from muth.domain import CreateSession, LearningFeedback
from muth.engines.biometric import Calibration
from muth.errors import MuthError
from muth.learning import (
    Gates,
    LearningService,
    cohort,
    refinement_loop,
    subject_hash,
    wilson_upper,
)
from muth.main import create_app
from muth.media import decode_image
from muth.security import Principal
from muth.storage import CalibrationRow, Database, LearningSampleRow, SessionStore
from tests.test_muth import png
from tests.test_sessions import PAYLOAD, settings


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.config = settings().model_copy(update={"engine_mode": "biometric"})
        self.db = Database(self.config)
        self.addCleanup(self.db.engine.dispose)
        self.store = SessionStore(self.db, self.config)
        self.principal = Principal("alpha", "alpha-key", frozenset(SCOPES))
        self.engine = SimpleNamespace(fingerprint="a" * 64, calibration=Calibration(0.8))
        self.runtime = SimpleNamespace(face=self.engine, liveness=self.engine)
        self.service = LearningService(self.store, self.runtime)
        app = create_app(settings())
        self.addCleanup(app.state.database.engine.dispose)
        image = decode_image(png(), self.config)
        self.result = app.state.verify_service.verify(image, image)
        for _role, check, kind in [
            ("face", self.result.checks.face_match, "cosine_similarity"),
            ("liveness", self.result.checks.liveness, "liveness_softmax"),
        ]:
            check.score = 0.9
            check.score_kind = kind
            check.model_fingerprint = self.engine.fingerprint

    def complete(self, opt_in=True, subject="customer-1"):
        payload = CreateSession.model_validate(
            {
                **PAYLOAD,
                "subject_reference": subject,
                "consent": {**PAYLOAD["consent"], "learning_opt_in": opt_in},
            }
        )
        sid = self.store.create(self.principal, payload, "a" * 32).session_id
        claim = self.store.claim(self.principal, sid, "attempt-one", "fp", "b" * 32)
        self.store.complete(self.principal, sid, claim.attempt, self.result, "c" * 32)
        return sid

    def feedback(self, sid, **changes):
        value = {"role": "face", "label": "genuine", "source_reference": "review-12345", **changes}
        return self.service.feedback(self.principal, sid, LearningFeedback(**value), "d" * 32)

    def seed(self, count=400, bad_test=False, role="face"):
        """Synthetic labelled scores test gates; they measure no real model accuracy."""
        with self.db.transaction() as db:
            for split, n in [("calibration", 50), ("validation", count), ("test", count)]:
                for positive in (True, False):
                    for i in range(n):
                        sample_id = f"{role}-{split}-{positive}-{i}"
                        score = 0.9 if positive else 0.1
                        if bad_test and split == "test" and not positive:
                            score = 0.95
                        data = {
                            "score": score,
                            "device_group": "unknown",
                            "pair": sample_id,
                            "label": ("genuine" if positive else "impostor")
                            if role == "face"
                            else ("live" if positive else "spoof"),
                            "attack_type": "print" if not positive and role == "liveness" else None,
                        }
                        db.add(
                            LearningSampleRow(
                                sample_id=sample_id,
                                tenant_id="alpha",
                                session_id=sample_id,
                                role=role,
                                model_fingerprint=self.engine.fingerprint,
                                subject_hash=sample_id,
                                split=split,
                                state="labelled",
                                payload=self.store._encrypt(json.dumps(data)),
                                created_at=i,
                                retain_until=self.store.clock() + 10000,
                            )
                        )

    def test_opt_in_demo_and_replay_guards(self):
        self.complete(opt_in=False)
        with self.db.transaction() as db:
            self.assertEqual(len(list(db.scalars(select(LearningSampleRow)))), 0)
        sid = self.complete()
        self.assertIsNotNone(
            self.store.claim(self.principal, sid, "attempt-one", "fp", "z" * 32).replay
        )
        with self.db.transaction() as db:
            rows = list(db.scalars(select(LearningSampleRow)))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all("score" not in r.payload for r in rows))
        self.store.settings = settings()
        self.complete()
        with self.db.transaction() as db:
            self.assertEqual(len(list(db.scalars(select(LearningSampleRow)))), 2)

    def test_independent_labels_immutable_and_official_source_blocked(self):
        sid = self.complete()
        first = self.feedback(sid)
        self.assertEqual(first["state"], "labelled")
        self.assertEqual(self.feedback(sid)["sample_id"], first["sample_id"])
        with self.assertRaises(MuthError) as error:
            self.feedback(sid, label="impostor", capture_subject_reference="person-other")
        self.assertEqual(error.exception.code, "label_conflict")
        with self.assertRaises(MuthError) as error:
            self.feedback(sid, source="official_verified")
        self.assertEqual(error.exception.code, "source_not_integrated")

    def test_subject_disjoint_split_and_cross_split_impostor_exclusion(self):
        expected_split = cohort(subject_hash(self.store, "alpha", "customer-1"))
        other = next(
            f"other-{i}"
            for i in range(100)
            if cohort(subject_hash(self.store, "alpha", f"other-{i}")) != expected_split
        )
        sid = self.complete()
        response = self.feedback(sid, label="impostor", capture_subject_reference=other)
        self.assertEqual(response["state"], "excluded")
        with self.assertRaises(MuthError):
            self.feedback(self.complete(), capture_subject_reference="different-person")

    def test_injection_labels_cannot_certify_rgb_liveness(self):
        response = self.feedback(
            self.complete(), role="liveness", label="spoof", attack_type="injection"
        )
        self.assertEqual(response["state"], "excluded")

    def test_not_enough_data_and_wilson_bound(self):
        self.seed(count=30)
        self.assertEqual(self.service.run("alpha")["face"]["status"], "collecting")
        self.assertGreater(wilson_upper(0, 30), 0.01)
        self.assertLess(wilson_upper(0, 400), 0.01)

    def test_promotion_and_model_tenant_isolation(self):
        self.seed()
        result = self.service.run("alpha")["face"]
        self.assertEqual(result["status"], "promoted", result)
        self.assertTrue(self.service.calibration("alpha", "face", self.engine).validated)
        self.assertFalse(self.service.calibration("beta", "face", self.engine).validated)
        other = SimpleNamespace(fingerprint="b" * 64, calibration=Calibration(0.8))
        self.assertFalse(self.service.calibration("alpha", "face", other).validated)
        self.assertEqual(self.service.run("alpha")["face"]["status"], "unchanged")

    def test_holdout_failure_blocks_promotion(self):
        self.seed(bad_test=True)
        result = self.service.run("alpha")["face"]
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(self.service.calibration("alpha", "face", self.engine).validated)
        self.assertTrue(any("test" in f for f in result["failures"]))

    def test_deletion_revokes_policy_and_rollback_cannot_resurrect(self):
        self.seed()
        policy_id = self.service.run("alpha")["face"]["policy_id"]
        from muth.learning import erase_samples

        with self.db.transaction() as db:
            erase_samples(db, ["face-test-True-0"])
        self.assertFalse(self.service.calibration("alpha", "face", self.engine).validated)
        with self.assertRaises(MuthError):
            self.service.rollback("alpha", "face", policy_id)
        with self.db.transaction() as db:
            self.assertEqual(db.get(CalibrationRow, policy_id).state, "revoked")

    def test_withdraw_and_retention_remove_learning_data(self):
        sid = self.complete()
        self.service.withdraw(self.principal, sid, "f" * 32)
        self.assertFalse(self.store.get(self.principal, sid).consent.learning_opt_in)
        with self.db.transaction() as db:
            self.assertEqual(len(list(db.scalars(select(LearningSampleRow)))), 0)
        self.complete()
        now = self.store.clock()
        self.store.clock = lambda: now + 31 * 86400
        self.store.purge()
        with self.db.transaction() as db:
            self.assertEqual(len(list(db.scalars(select(LearningSampleRow)))), 0)

    def test_liveness_attack_metrics_and_tiny_gate_test_configuration(self):
        self.seed(role="liveness")
        result = self.service.run("alpha")["liveness"]
        self.assertEqual(result["status"], "promoted", result)
        report = self.service.status("alpha")["policies"][0]["report"]
        self.assertIn("attack:print", report["test"])
        self.assertEqual(Gates().min_evaluation_per_class, 400)

    def test_expired_member_blocks_existing_policy_without_purge(self):
        self.seed()
        self.service.run("alpha")
        with self.db.transaction() as db:
            db.get(LearningSampleRow, "face-test-True-0").retain_until = self.store.clock() - 1
        self.assertFalse(self.service.calibration("alpha", "face", self.engine).validated)

    def test_unsupported_device_does_not_inherit_validated_policy(self):
        self.seed()
        self.service.run("alpha")
        self.assertFalse(self.service.calibration("alpha", "face", self.engine, "ios").validated)

    def test_repeated_subjects_do_not_meet_sample_minimum(self):
        self.seed()
        with self.db.transaction() as db:
            for sample in db.scalars(select(LearningSampleRow)):
                data = json.loads(self.store._decrypt(sample.payload))
                data["pair"] = "same-identity-for-every-capture"
                sample.payload = self.store._encrypt(json.dumps(data))
        self.assertEqual(self.service.run("alpha")["face"]["status"], "collecting")

    def test_refinement_does_not_promote_equal_performance(self):
        self.seed()
        self.service.run("alpha")
        with self.db.transaction() as db:
            original = db.get(LearningSampleRow, "face-calibration-True-0")
            db.add(
                LearningSampleRow(
                    sample_id="new-independent-genuine",
                    tenant_id="alpha",
                    session_id="extra-session",
                    role="face",
                    model_fingerprint=self.engine.fingerprint,
                    subject_hash="new-subject",
                    split="calibration",
                    state="labelled",
                    payload=self.store._encrypt(
                        json.dumps(
                            {
                                "score": 0.91,
                                "device_group": "unknown",
                                "pair": "new-subject",
                                "label": "genuine",
                            }
                        )
                    ),
                    created_at=1000,
                    retain_until=original.retain_until,
                )
            )
        result = self.service.run("alpha")["face"]
        self.assertEqual(result["status"], "blocked", result)
        self.assertIn("no_measurable_improvement", result["failures"])

    def test_holdout_budget_cannot_be_reset_with_new_samples(self):
        self.seed()
        self.service.gates = Gates(max_attempts_per_test_panel=1)
        self.service.run("alpha")
        with self.db.transaction() as db:
            sample = db.get(LearningSampleRow, "face-test-True-0")
            data = json.loads(self.store._decrypt(sample.payload))
            db.add(
                LearningSampleRow(
                    sample_id="later-test-example",
                    tenant_id="alpha",
                    session_id="extra-session",
                    role="face",
                    model_fingerprint=self.engine.fingerprint,
                    subject_hash="new-subject",
                    split="test",
                    state="labelled",
                    payload=self.store._encrypt(json.dumps({**data, "pair": "new-subject"})),
                    created_at=1000,
                    retain_until=sample.retain_until,
                )
            )
        self.assertEqual(self.service.run("alpha")["face"]["status"], "holdout_budget_exhausted")


class PersistentPromotionTests(unittest.TestCase):
    def test_two_connections_promote_a_snapshot_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            config = settings(
                database_url=f"sqlite:///{folder}/muth.db",
                data_key=SecretStr(Fernet.generate_key().decode()),
            )
            database = Database(config)
            self.addCleanup(database.engine.dispose)
            database.migrate()
            self.db = database
            self.store = SessionStore(database, config)
            self.engine = SimpleNamespace(fingerprint="a" * 64, calibration=Calibration(0.8))
            runtime = SimpleNamespace(face=self.engine, liveness=self.engine)
            first = LearningService(self.store, runtime)
            LearningTests.seed(self)
            other_db = Database(config)
            self.addCleanup(other_db.engine.dispose)
            second = LearningService(SessionStore(other_db, config), runtime)
            barrier = threading.Barrier(2)

            def run(service):
                barrier.wait(timeout=5)
                return service.run("alpha")["face"]["status"]

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(run, [first, second]))
            self.assertCountEqual(results, ["promoted", "unchanged"])
            self.assertTrue(second.calibration("alpha", "face", self.engine).validated)


class AutomaticRefinementTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_worker_refines_all_tenants_and_cancels(self):
        service = SimpleNamespace(
            store=SimpleNamespace(db=SimpleNamespace(ready=lambda: True)),
            run=Mock(return_value={"status": "collecting"}),
        )
        pauses = []

        async def pause(interval):
            pauses.append(interval)
            if len(pauses) == 2:
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await refinement_loop(service, 300, {"alpha", "beta"}, pause=pause)
        self.assertEqual(pauses, [300, 300])
        self.assertCountEqual([c.args[0] for c in service.run.call_args_list], ["alpha", "beta"])
