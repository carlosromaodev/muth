"""Browser contributions are consented proposals until an independent review."""

import json
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select

from muth.capture_learning import CaptureLearningRow
from muth.document_models import DocumentData, DocumentField
from muth.main import create_app
from muth.storage import LearningSampleRow, SessionRow
from tests.test_capture_api import png
from tests.test_sessions import key, settings

PRIVATE_NAME = "PESSOA FICTICIA OCR 84621"
PRIVATE_NUMBER = "123456789LA042"


class DeterministicDocumentOCR:
    """Exercise persistence/contracts independently of installed OCR binaries."""

    def extract(self, front, back):
        return DocumentData(
            status="partial",
            document_type="bi",
            issuing_country="AO",
            fields={
                "name": DocumentField(value=PRIVATE_NAME, confidence=0.91, source_side="front"),
                "document_number": DocumentField(
                    value=PRIVATE_NUMBER, confidence=0.89, source_side="back"
                ),
            },
            reasons=["test_document_fields"],
            ocr_provider="test-double",
            ocr_version="fixture-v1",
        )


class WebLearningApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(settings())
        self.app.state.document_ocr = DeterministicDocumentOCR()
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.operator_headers = {"X-API-Key": "alpha-token"}

    def create(self, *, opt_in=False, consent_changes=None, **changes):
        consent = {
            "accepted": True,
            "purpose": "onboarding",
            "policy_version": "capture-privacy-v1",
        }
        if opt_in:
            consent.update(learning_opt_in=True, learning_policy_version="capture-learning-v1")
        consent.update(consent_changes or {})
        return self.client.post(
            "/capture-api/sessions", json={"consent": consent, "device_group": "web"} | changes
        )

    @staticmethod
    def bearer(session):
        return {"Authorization": f"Bearer {session['capture_token']}"}

    @staticmethod
    def path(session, suffix=""):
        return f"/capture-api/sessions/{session['session_id']}{suffix}"

    def verify(self, session):
        return self.client.post(
            self.path(session, "/verify"),
            headers=self.bearer(session) | {"Idempotency-Key": "learning-web-001"},
            files={
                "document_front": ("front.png", png(color="red"), "image/png"),
                "document_back": ("back.png", png(color="blue"), "image/png"),
                "selfie": ("selfie.png", png(color="green"), "image/png"),
            },
        )

    def result(self, session):
        return self.client.get(self.path(session), headers=self.bearer(session))

    def proposal(self, session, fields=None, **changes):
        return self.client.post(
            self.path(session, "/document-corrections"),
            headers=self.bearer(session),
            json={"fields": fields or {"name": "NOME PROPOSTO"}} | changes,
        )

    def queue(self):
        response = self.client.get("/v1/capture-learning", headers=self.operator_headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def review(self, session, **changes):
        payload = {
            "independent_evidence_confirmed": True,
            "subject_reference": "independent-registry:person-84621",
            "source_reference": "evidence-84621",
            "document_fields": {"name": "NOME CONFIRMADO"},
        }
        return self.client.post(
            f"/v1/capture-learning/{session['session_id']}/review",
            headers=self.operator_headers,
            json=payload | changes,
        )

    def samples(self):
        with self.app.state.database.transaction() as db:
            return list(db.scalars(select(LearningSampleRow)))

    def assert_no_sensitive_error(self, response):
        self.assertNotIn(PRIVATE_NAME, response.text)
        self.assertNotIn(PRIVATE_NUMBER, response.text)
        self.assertNotIn("invalid-secret-subject", response.text)
        self.assertNotIn("traceback", response.text.lower())
        self.assertIn("error", response.json())

    def test_config_and_separate_versioned_learning_consent(self):
        config = self.client.get("/capture-api/config").json()
        self.assertTrue(config["learning_collection_enabled"])
        self.assertEqual(config["learning_policy_version"], "capture-learning-v1")
        self.assertGreater(config["learning_retention_days"], config["retention_days"])
        self.assertFalse(config["document_authenticity_supported"])
        for consent in (
            {"learning_opt_in": True},
            {"learning_opt_in": True, "learning_policy_version": "old-version"},
            {"learning_opt_in": "true", "learning_policy_version": "capture-learning-v1"},
        ):
            with self.subTest(consent=consent):
                response = self.create(consent_changes=consent)
                self.assertEqual(response.status_code, 422)
                self.assert_no_sensitive_error(response)
        self.assertEqual(self.create(opt_in=True).status_code, 201)

    def test_capture_deadline_and_service_pause_do_not_block_consent_withdrawal(self):
        session = self.create(opt_in=True).json()
        self.verify(session)
        now = self.app.state.store.clock()
        self.app.state.store.clock = lambda: now + 2 * 3600
        self.app.state.settings.capture_portal_enabled = False
        self.app.state.settings.engine_mode = "disabled"
        self.assertEqual(self.result(session).status_code, 200)
        response = self.client.delete(
            self.path(session, "/learning-consent"), headers=self.bearer(session)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["learning"]["status"], "withdrawn")
        self.assertEqual(self.samples(), [])

    def test_default_opt_out_returns_document_data_without_learning(self):
        session = self.create().json()
        report = self.verify(session)
        self.assertEqual(report.status_code, 200, report.text)
        document = report.json()["document_data"]
        self.assertEqual(document["fields"]["name"]["value"], PRIVATE_NAME)
        self.assertEqual(document["fields"]["document_number"]["source_side"], "back")
        self.assertFalse(document["authenticity_confirmed"])
        self.assertEqual(report.json()["learning"]["status"], "not_opted_in")
        self.assertFalse(report.json()["learning"]["consented"])
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.samples(), [])
        self.assertEqual(self.proposal(session).status_code, 409)
        with self.app.state.database.transaction() as db:
            self.assertIsNone(db.get(CaptureLearningRow, session["session_id"]))
            row = db.get(SessionRow, session["session_id"])
            self.assertNotIn(PRIVATE_NAME, row.capture_result)
            self.assertNotIn(PRIVATE_NUMBER, row.capture_result)

    def test_opt_in_pending_contribution_is_encrypted_and_has_no_training_samples(self):
        session = self.create(opt_in=True).json()
        response = self.verify(session)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["learning"]["status"], "pending_review")
        self.assertEqual(response.json()["learning"]["samples"], 0)
        self.assertEqual(self.samples(), [])
        queue = self.queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["session_id"], session["session_id"])
        self.assertEqual(queue[0]["document_fields"]["name"]["value"], PRIVATE_NAME)
        self.assertEqual(queue[0]["proposed_fields"], {})
        self.assertIsNone(queue[0]["review"])
        self.assertNotIn("capture_token", json.dumps(queue))
        with self.app.state.database.transaction() as db:
            candidate = db.get(CaptureLearningRow, session["session_id"])
            self.assertEqual(candidate.state, "pending_review")
            self.assertNotIn(PRIVATE_NAME, candidate.payload)
            self.assertNotIn(PRIVATE_NUMBER, candidate.payload)
            original = db.get(SessionRow, session["session_id"])
            self.assertNotIn(PRIVATE_NAME, original.capture_result)

    def test_browser_corrections_are_proposals_and_do_not_change_ocr_or_rating(self):
        session = self.create(opt_in=True).json()
        original = self.verify(session).json()
        response = self.proposal(session, {"name": "NOME PROPOSTO", "birth_date": "1990-06-12"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["learning"]["status"], "pending_review")
        report = self.result(session).json()
        self.assertEqual(report["document_data"], original["document_data"])
        self.assertEqual(report["score"], original["score"])
        queue = self.queue()
        self.assertEqual(queue[0]["proposed_fields"]["name"], "NOME PROPOSTO")
        self.assertEqual(queue[0]["document_fields"]["name"]["value"], PRIVATE_NAME)
        self.assertIsNone(queue[0]["review"])
        self.assertEqual(self.samples(), [])

    def test_correction_rejects_labels_subjects_unknown_fields_and_unsafe_text(self):
        session = self.create(opt_in=True).json()
        self.verify(session)
        invalid = (
            ({"fields": {"identity_score": "10"}}),
            ({"fields": {"name": PRIVATE_NAME}, "face_label": "genuine"}),
            ({"fields": {"name": PRIVATE_NAME}, "subject_reference": "invalid-secret-subject"}),
            ({"fields": {"birth_date": "31/12/1990"}}),
            ({"fields": {"name": "bad\x00text"}}),
            ({"fields": {"name": 123}}),
            ({"fields": {}}),
        )
        for payload in invalid:
            with self.subTest(payload=payload):
                response = self.client.post(
                    self.path(session, "/document-corrections"),
                    headers=self.bearer(session),
                    json=payload,
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_no_sensitive_error(response)
        self.assertEqual(self.queue()[0]["proposed_fields"], {})
        self.assertEqual(self.samples(), [])

    def test_new_browser_mutations_require_own_session_token_and_same_origin(self):
        one, two = self.create(opt_in=True).json(), self.create(opt_in=True).json()
        self.verify(one)
        self.verify(two)
        for suffix, method, payload in (
            ("/document-corrections", "POST", {"fields": {"name": PRIVATE_NAME}}),
            ("/learning-consent", "DELETE", None),
        ):
            with self.subTest(suffix=suffix):
                response = self.client.request(
                    method, self.path(two, suffix), headers=self.bearer(one), json=payload
                )
                self.assertEqual(response.status_code, 401)
                for origin in ("https://attacker.example", "null", "http://["):
                    response = self.client.request(
                        method,
                        self.path(one, suffix),
                        headers=self.bearer(one) | {"Origin": origin},
                        json=payload,
                    )
                    self.assertEqual(response.status_code, 403)
                    self.assertNotIn(one["capture_token"], response.text)
                    self.assert_no_sensitive_error(response)
        response = self.client.post(
            self.path(one, "/document-corrections"),
            headers=self.bearer(one),
            data={"fields": PRIVATE_NAME},
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(len(self.queue()), 2)

    def test_operator_review_requires_explicit_capture_review_scope(self):
        session = self.create(opt_in=True).json()
        self.verify(session)
        unprivileged = key("restricted", "restricted-token", {"feedback", "learning", "read"})
        self.app.state.settings.tenants.append(unprivileged)
        paths = (
            ("GET", "/v1/capture-learning", None),
            ("GET", "/v1/capture-learning/status", None),
            ("POST", "/v1/capture-learning/refine", None),
            ("POST", f"/v1/capture-learning/{session['session_id']}/review", {}),
        )
        for method, path, payload in paths:
            for token in ("reader-token", "restricted-token"):
                with self.subTest(path=path, token=token):
                    response = self.client.request(
                        method, path, headers={"X-API-Key": token}, json=payload
                    )
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assert_no_sensitive_error(response)
            response = self.client.request(method, path, headers=self.bearer(session), json=payload)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.samples(), [])
        self.assertEqual(len(self.queue()), 1)

    def test_review_requires_independent_evidence_and_namespaced_known_labels(self):
        session = self.create(opt_in=True).json()
        self.verify(session)
        invalid = (
            {"independent_evidence_confirmed": False},
            {"independent_evidence_confirmed": "true"},
            {"subject_reference": "invalid-secret-subject"},
            {"source_reference": "private source with spaces"},
            {"face_label": "approved"},
            {"liveness_label": "authentic"},
            {"face_label": "impostor"},
            {
                "face_label": "impostor",
                "capture_subject_reference": "independent-registry:person-84621",
            },
            {
                "face_label": "genuine",
                "capture_subject_reference": "independent-registry:different-person",
            },
            {"liveness_label": "spoof"},
            {"liveness_label": "live", "attack_type": "screen_replay"},
            {"document_fields": {"unsupported": PRIVATE_NAME}},
            {"document_fields": {}},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                response = self.review(session, **changes)
                self.assertEqual(response.status_code, 422, response.text)
                self.assert_no_sensitive_error(response)
        self.assertEqual(self.result(session).json()["learning"]["status"], "pending_review")
        self.assertEqual(self.samples(), [])

    def test_demo_ocr_review_never_trains_and_replay_reports_current_learning_status(self):
        session = self.create(opt_in=True).json()
        original = self.verify(session).json()
        response = self.review(session, face_label="genuine", liveness_label="live")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "reviewed")
        self.assertEqual(response.json()["samples"], 0)
        self.assertEqual(self.samples(), [])
        self.assertEqual(self.queue(), [])
        for report in (self.result(session).json(), self.verify(session).json()):
            self.assertEqual(report["learning"]["status"], "reviewed")
            self.assertEqual(report["document_data"], original["document_data"])
            self.assertEqual(report["score"], original["score"])
            self.assertFalse(report["score"]["authenticity_confirmed"])
        self.assertEqual(
            self.review(session, face_label="genuine", liveness_label="live").status_code, 200
        )
        self.assertEqual(
            self.review(session, document_fields={"name": "ALTERADO"}).status_code, 409
        )

    def test_withdrawal_erases_pending_or_reviewed_data_and_preserves_original_result(self):
        for reviewed in (False, True):
            with self.subTest(reviewed=reviewed):
                session = self.create(opt_in=True).json()
                original = self.verify(session).json()
                self.proposal(session)
                if reviewed:
                    self.assertEqual(self.review(session).status_code, 200)
                now = self.app.state.store.clock()
                response = self.client.delete(
                    self.path(session, "/learning-consent"), headers=self.bearer(session)
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["learning"]["status"], "withdrawn")
                self.assertFalse(response.json()["learning"]["consented"])
                self.assertEqual(response.json()["proposed_fields"], {})
                report = self.result(session).json()
                self.assertEqual(report["document_data"], original["document_data"])
                self.assertEqual(report["score"], original["score"])
                self.assertEqual(report["learning"]["status"], "withdrawn")
                self.assertEqual(self.verify(session).json()["learning"]["status"], "withdrawn")
                self.assertEqual(self.proposal(session).status_code, 409)
                self.assertEqual(self.review(session).status_code, 409)
                with self.app.state.database.transaction() as db:
                    candidate = db.get(CaptureLearningRow, session["session_id"])
                    self.assertEqual(candidate.state, "withdrawn")
                    self.assertIsNone(candidate.payload)
                    row = db.get(SessionRow, session["session_id"])
                    self.assertLessEqual(row.retain_until, now + 86400 + 1)
                    payload = json.loads(self.app.state.store._decrypt(row.payload))
                    self.assertFalse(payload["consent"]["learning_opt_in"])
                    self.assertIsNone(payload["subject_reference"])
                self.assertEqual(self.samples(), [])
        self.assertEqual(self.queue(), [])

    def test_session_deletion_and_retention_purge_erase_candidates_and_fields(self):
        for reviewed in (False, True):
            session = self.create(opt_in=True).json()
            self.verify(session)
            if reviewed:
                self.review(session)
            response = self.client.delete(self.path(session), headers=self.bearer(session))
            self.assertEqual(response.status_code, 204)
            with self.app.state.database.transaction() as db:
                self.assertIsNone(db.get(CaptureLearningRow, session["session_id"]))
                row = db.get(SessionRow, session["session_id"])
                self.assertIsNone(row.capture_result)
                self.assertIsNone(row.payload)
            self.assertEqual(self.result(session).status_code, 404)
        session = self.create(opt_in=True).json()
        self.verify(session)
        self.app.state.store.clock = lambda: 10**12
        self.assertGreaterEqual(self.app.state.capture_store.purge(), 1)
        self.app.state.capture_learning.purge()
        with self.app.state.database.transaction() as db:
            self.assertIsNone(db.get(CaptureLearningRow, session["session_id"]))
            self.assertIsNone(db.get(SessionRow, session["session_id"]).capture_result)
        self.assertEqual(self.samples(), [])
        self.assertEqual(self.queue(), [])

    def test_contribution_collection_can_be_disabled_independently(self):
        self.app.state.settings.capture_learning_enabled = False
        config = self.client.get("/capture-api/config").json()
        self.assertFalse(config["learning_collection_enabled"])
        response = self.create(opt_in=True)
        self.assertEqual(response.status_code, 422)
        self.assert_no_sensitive_error(response)
        session = self.create().json()
        result = self.verify(session)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["learning"]["status"], "unavailable")
        self.assertEqual(result.json()["document_data"]["fields"]["name"]["value"], PRIVATE_NAME)
        response = self.client.post("/v1/capture-learning/refine", headers=self.operator_headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.queue(), [])

    def test_consent_precedes_capture_and_awaiting_candidate_cannot_be_corrected(self):
        session = self.create(opt_in=True).json()
        self.assertEqual(self.proposal(session).status_code, 409)
        self.assertEqual(self.review(session).status_code, 409)
        self.assertEqual(self.result(session).status_code, 409)
        self.assertEqual(self.queue(), [])
        with self.app.state.database.transaction() as db:
            candidate = db.get(CaptureLearningRow, session["session_id"])
            self.assertEqual(candidate.state, "awaiting_capture")
            row = db.get(SessionRow, session["session_id"])
            self.assertGreater(row.retain_until, self.app.state.store.clock() + 86400)
