"""Consented template registration and purpose-separated HTTP capabilities."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from muth.engines.biometric import Calibration
from muth.engines.bundle import EngineBundle
from muth.identity_store import IdentityRow
from muth.main import create_app
from muth.storage import SessionRow
from tests.test_capture_scoring import StubDocument, document, measurement
from tests.test_sessions import settings
from tests.test_web_learning_api import DeterministicDocumentOCR


class Vector(list):
    def tolist(self):
        return list(self)


class Face:
    fingerprint = "a" * 64
    calibration = Calibration(0.363)
    core = SimpleNamespace(embedding=lambda image: (Vector([1.0] + [0.0] * 127), {}))

    def compare(self, reference, selfie):
        return measurement("face", 0.99).model_copy(update={"model_fingerprint": self.fingerprint})


class PAD:
    calibration = Calibration(0.8)

    def assess(self, selfie):
        return measurement("liveness", 0.99).model_copy(update={"model_fingerprint": "b" * 64})


class IdentityApiTests(unittest.TestCase):
    def setUp(self):
        # Controlled engines exercise HTTP/storage independently of native models.
        self.app = create_app(settings(document_ocr_enabled=False))
        self.app.state.settings.engine_mode = "biometric"
        self.app.state.runtime = object()
        self.app.state.engines = EngineBundle(Face(), PAD(), StubDocument())
        self.app.state.document_ocr = DeterministicDocumentOCR()
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def create(self, opt_in=True, **extra):
        consent = {
            "accepted": True,
            "purpose": "onboarding",
            "policy_version": "capture-privacy-v1",
            "identity_enrollment_opt_in": opt_in,
        }
        if opt_in:
            consent["identity_enrollment_policy_version"] = "identity-enrollment-v1"
        consent.update(extra)
        return self.client.post("/capture-api/sessions", json={"consent": consent})

    def verify(self, session):
        return self.client.post(
            f"/capture-api/sessions/{session['session_id']}/verify",
            headers={
                "Authorization": "Bearer " + session["capture_token"],
                "Idempotency-Key": "identity-api-001",
            },
            files={
                "document_front": ("front.png", document(0).content, "image/png"),
                "document_back": ("back.png", document(1).content, "image/png"),
                "selfie": ("selfie.png", document(0).content, "image/png"),
            },
        )

    def enroll(self):
        session = self.create().json()
        response = self.verify(session)
        self.assertEqual(response.status_code, 200, response.text)
        registration = response.json()["identity"]
        self.assertEqual(registration["status"], "enrolled_provisional", registration)
        return session, registration

    @staticmethod
    def identity_headers(identity):
        return {"Authorization": "Bearer " + identity["identity_token"]}

    def test_enroll_replay_private_template_and_capability_not_persisted(self):
        session, identity = self.enroll()
        replay = self.verify(session).json()["identity"]
        self.assertEqual(replay["identity_id"], identity["identity_id"])
        with self.app.state.database.transaction() as db:
            rows = list(db.scalars(select(IdentityRow)))
            self.assertEqual(len(rows), 1)
            self.assertNotIn("embedding", rows[0].payload)
            row = db.get(SessionRow, session["session_id"])
            self.assertNotIn(
                identity["identity_token"], self.app.state.store._decrypt(row.capture_result)
            )
        profile = self.client.get(
            "/identity-api/identities/" + identity["identity_id"],
            headers=self.identity_headers(identity),
        )
        self.assertEqual(profile.status_code, 200)
        self.assertNotIn("embedding", profile.text)
        self.assertFalse(profile.json()["authenticity_confirmed"])

    def test_missing_or_wrong_policy_no_enrollment_and_strict_opt_in(self):
        self.assertEqual(self.create(identity_enrollment_policy_version="old").status_code, 422)
        self.assertEqual(self.create(identity_enrollment_opt_in="true").status_code, 422)
        session = self.create(False).json()
        self.assertEqual(self.verify(session).json()["identity"]["status"], "not_requested")
        with self.app.state.database.transaction() as db:
            self.assertEqual(list(db.scalars(select(IdentityRow))), [])

    def test_tokens_cannot_cross_profile_purpose_b2b_or_other_identity(self):
        session, identity = self.enroll()
        path = "/identity-api/identities/" + identity["identity_id"]
        for headers in (
            {},
            {"Authorization": "Bearer " + session["capture_token"]},
            {"X-API-Key": "alpha-token"},
        ):
            self.assertEqual(self.client.get(path, headers=headers).status_code, 401)
        self.assertEqual(
            self.client.get(
                "/capture-api/sessions/" + session["session_id"],
                headers=self.identity_headers(identity),
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/identity-api/identities/mth_idn_" + "0" * 32,
                headers=self.identity_headers(identity),
            ).status_code,
            401,
        )

    def test_1_to_1_comparison_and_operator_auth_remain_provisional(self):
        _, identity = self.enroll()
        files = {"selfie": ("selfie.png", document().content, "image/png")}
        response = self.client.post(
            "/identity-api/identities/" + identity["identity_id"] + "/compare",
            headers=self.identity_headers(identity),
            files=files,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["face_match"]["score"], 1)
        self.assertFalse(response.json()["authenticated"])
        self.assertEqual(response.json()["face_match"]["outcome"], "inconclusive")
        operator = self.client.post(
            "/v1/auth/authenticate",
            params={"identity_id": identity["identity_id"]},
            headers={"X-API-Key": "alpha-token"},
            files=files,
        )
        self.assertEqual(operator.status_code, 200, operator.text)
        self.assertFalse(operator.json()["authenticated"])

    def test_model_version_change_requires_new_enrollment(self):
        _, identity = self.enroll()
        self.app.state.engines.face.fingerprint = "c" * 64
        response = self.client.post(
            "/identity-api/identities/" + identity["identity_id"] + "/compare",
            headers=self.identity_headers(identity),
            files={"selfie": ("x.png", document().content, "image/png")},
        )
        self.assertIsNone(response.json()["face_match"]["score"])
        self.assertIn("identity_model_version_mismatch", response.json()["face_match"]["reasons"])

    def test_calibrated_match_does_not_authenticate_provisional_document(self):
        _, identity = self.enroll()
        self.app.state.engines.face.calibration = Calibration(0.363, "locally-validated", True)
        response = self.client.post(
            "/identity-api/identities/" + identity["identity_id"] + "/compare",
            headers=self.identity_headers(identity),
            files={"selfie": ("x.png", document().content, "image/png")},
        )
        self.assertEqual(response.json()["face_match"]["outcome"], "pass")
        self.assertFalse(response.json()["authenticated"])

    def test_policy_changed_during_capture_prevents_enrollment_keeps_result(self):
        session = self.create().json()
        self.app.state.settings.identity_enrollment_policy_version = "new-policy-v2"
        response = self.verify(session)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["identity"]["status"], "unavailable")
        self.assertIn("name", response.json()["document_data"]["fields"])
        with self.app.state.database.transaction() as db:
            self.assertEqual(list(db.scalars(select(IdentityRow))), [])

    def test_profile_quota_preserves_completed_verification(self):
        self.app.state.settings.identity_max_profiles = 1
        _, identity = self.enroll()
        second = self.create().json()
        response = self.verify(second)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["identity"]["status"], "unavailable")
        self.assertIn("name", response.json()["document_data"]["fields"])
        self.assertIsNotNone(response.json()["score"]["value"])
        with self.app.state.database.transaction() as db:
            self.assertEqual(len(list(db.scalars(select(IdentityRow)))), 1)
        self.assertEqual(
            self.client.get(
                "/identity-api/identities/" + identity["identity_id"],
                headers=self.identity_headers(identity),
            ).status_code,
            200,
        )

    def test_expired_capture_and_paused_service_still_allow_profile_deletion(self):
        _, identity = self.enroll()
        now = self.app.state.store.clock()
        self.app.state.store.clock = lambda: now + 2 * 86400
        self.app.state.capture_store.purge()
        self.app.state.settings.engine_mode = "disabled"
        self.app.state.settings.capture_portal_enabled = False
        path = "/identity-api/identities/" + identity["identity_id"]
        self.assertEqual(
            self.client.get(path, headers=self.identity_headers(identity)).status_code, 200
        )
        self.assertEqual(
            self.client.delete(path, headers=self.identity_headers(identity)).status_code, 204
        )
        self.assertEqual(
            self.client.get(path, headers=self.identity_headers(identity)).status_code, 404
        )

    def test_explicit_session_deletion_removes_linked_template(self):
        session, identity = self.enroll()
        self.assertEqual(
            self.client.delete(
                "/capture-api/sessions/" + session["session_id"],
                headers={"Authorization": "Bearer " + session["capture_token"]},
            ).status_code,
            204,
        )
        self.assertEqual(
            self.client.get(
                "/identity-api/identities/" + identity["identity_id"],
                headers=self.identity_headers(identity),
            ).status_code,
            404,
        )
        with self.app.state.database.transaction() as db:
            self.assertIsNone(db.get(IdentityRow, identity["identity_id"]).payload)

    def test_origin_extra_files_and_unauthorized_body_are_rejected(self):
        _, identity = self.enroll()
        path = "/identity-api/identities/" + identity["identity_id"] + "/compare"
        with patch("muth.identity_api.Request.form") as parser:
            self.assertEqual(
                self.client.post(
                    path, headers={"Authorization": "Bearer invalid"}, content=b"private"
                ).status_code,
                401,
            )
            parser.assert_not_called()
        headers = self.identity_headers(identity) | {"Origin": "https://attacker.test"}
        self.assertEqual(
            self.client.post(path, headers=headers, content=b"private").status_code, 403
        )
        files = {
            "selfie": ("x.png", document().content, "image/png"),
            "extra": ("x.png", document().content, "image/png"),
        }
        self.assertEqual(
            self.client.post(
                path, headers=self.identity_headers(identity), files=files
            ).status_code,
            400,
        )
