import unittest
from io import BytesIO
from unittest.mock import Mock

from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr

from muth.config import Settings
from muth.domain import Check, Checks, DocumentCheck, Outcome, VerificationStatus
from muth.engines.id import DocumentAnalysis
from muth.main import create_app
from muth.media import ImageInput
from muth.services.decision import decide
from muth.services.verify import VerifyService


def png(size: tuple[int, int] = (32, 32)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, format="PNG")
    return buffer.getvalue()


def checks(outcomes: tuple[Outcome, Outcome, Outcome]) -> Checks:
    common = {"reasons": [], "provider": "test", "model_version": "test-v1"}
    return Checks(
        document=DocumentCheck(outcome=outcomes[0], **common),
        liveness=Check(outcome=outcomes[1], **common),
        face_match=Check(outcome=outcomes[2], **common),
    )


class DecisionTests(unittest.TestCase):
    def test_failure_in_any_check_rejects_even_with_uncertainty(self):
        for index in range(3):
            outcomes = [Outcome.INCONCLUSIVE] * 3
            outcomes[index] = Outcome.FAIL
            self.assertEqual(decide(checks(tuple(outcomes)))[0], VerificationStatus.REJECTED)

    def test_missing_evidence_in_any_check_requires_review(self):
        for index in range(3):
            outcomes = [Outcome.PASS] * 3
            outcomes[index] = Outcome.INCONCLUSIVE
            self.assertEqual(decide(checks(tuple(outcomes)))[0], VerificationStatus.REVIEW)

    def test_all_pass_only_approves_outside_demo(self):
        evidence = checks((Outcome.PASS,) * 3)
        self.assertEqual(decide(evidence)[0], VerificationStatus.APPROVED)
        self.assertEqual(decide(evidence, demo=True)[0], VerificationStatus.REVIEW)


class OrchestrationTests(unittest.TestCase):
    def test_face_comparison_uses_extracted_portrait_and_liveness_can_reject(self):
        document = ImageInput(b"document", 100, 100, "PNG")
        portrait = ImageInput(b"portrait", 20, 20, "PNG")
        selfie = ImageInput(b"selfie", 30, 30, "PNG")
        evidence = checks((Outcome.PASS, Outcome.FAIL, Outcome.PASS))
        face, liveness, identity = Mock(), Mock(), Mock()
        identity.analyze.return_value = DocumentAnalysis(evidence.document, portrait)
        face.compare.return_value = evidence.face_match
        liveness.assess.return_value = evidence.liveness
        service = VerifyService(face, liveness, identity, demo=False)
        result = service.verify(document, selfie)
        identity.analyze.assert_called_once_with(document)
        face.compare.assert_called_once_with(portrait, selfie)
        liveness.assess.assert_called_once_with(selfie)
        self.assertEqual(result.status, VerificationStatus.REJECTED)

    def test_missing_portrait_prevents_comparison_and_approval(self):
        image = ImageInput(b"image", 20, 20, "PNG")
        evidence = checks((Outcome.PASS, Outcome.PASS, Outcome.PASS))
        face, liveness, identity = Mock(), Mock(), Mock()
        identity.analyze.return_value = DocumentAnalysis(evidence.document)
        liveness.assess.return_value = evidence.liveness
        result = VerifyService(face, liveness, identity, demo=False).verify(image, image)
        face.compare.assert_not_called()
        self.assertEqual(result.status, VerificationStatus.REVIEW)
        self.assertIn("face_match:document_portrait_unavailable", result.reasons)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(
            create_app(
                Settings(_env_file=None, engine_mode="demo", api_key=SecretStr("test-secret"))
            ),
            headers={"X-API-Key": "test-secret"},
        )
        self.addCleanup(self.client.close)

    def verify(self, document: bytes, selfie: bytes | None = None):
        return self.client.post(
            "/v1/verifications",
            files={
                "document": ("bi.png", document, "image/png"),
                "selfie": ("selfie.png", selfie if selfie is not None else png(), "image/png"),
            },
        )

    def test_demo_verification_is_explicit_and_never_approves(self):
        first, second = self.verify(png()), self.verify(png())
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["status"], "review")
        self.assertEqual(body["mode"], "demo")
        self.assertIn("demo_not_identity_verification", body["reasons"])
        self.assertEqual(body["checks"]["document"]["fields"], {})
        self.assertIsNone(body["checks"]["face_match"]["score"])
        self.assertNotEqual(body["verification_id"], second.json()["verification_id"])

    def test_disabled_engines_fail_closed(self):
        with TestClient(
            create_app(
                Settings(_env_file=None, engine_mode="disabled", api_key=SecretStr("test-secret"))
            ),
            headers={"X-API-Key": "test-secret"},
        ) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/health/ready").status_code, 503)
            response = client.post("/v1/verifications", files={"document": png(), "selfie": png()})
            self.assertEqual(response.status_code, 503)

    def test_api_key_required_when_configured(self):
        app = create_app(
            Settings(_env_file=None, engine_mode="demo", api_key=SecretStr("test-secret"))
        )
        with TestClient(app) as client:
            self.assertEqual(client.post("/v1/auth/authenticate").status_code, 401)
            self.assertEqual(
                client.post("/v1/auth/authenticate", headers={"X-API-Key": "wrong"}).status_code,
                401,
            )
            self.assertEqual(
                client.post(
                    "/v1/auth/authenticate", headers={"X-API-Key": "test-secret"}
                ).status_code,
                501,
            )

    def test_fake_mime_type_does_not_bypass_validation(self):
        self.assertEqual(self.verify(b"not an image").status_code, 422)
        self.assertEqual(self.verify(b"").status_code, 422)

    def test_both_files_are_required(self):
        response = self.client.post("/v1/verifications", files={"document": png()})
        self.assertEqual(response.status_code, 422)

    def test_oversize_upload_rejected(self):
        self.client = TestClient(
            create_app(
                Settings(
                    _env_file=None,
                    engine_mode="demo",
                    max_upload_bytes=10,
                    api_key=SecretStr("test-secret"),
                )
            ),
            headers={"X-API-Key": "test-secret"},
        )
        self.assertEqual(self.verify(png()).status_code, 413)

    def test_excessive_resolution_rejected(self):
        self.client = TestClient(
            create_app(
                Settings(
                    _env_file=None,
                    engine_mode="demo",
                    max_image_pixels=100,
                    api_key=SecretStr("test-secret"),
                )
            ),
            headers={"X-API-Key": "test-secret"},
        )
        self.assertEqual(self.verify(png()).status_code, 413)

    def test_unsupported_format_rejected(self):
        buffer = BytesIO()
        Image.new("RGB", (8, 8)).save(buffer, format="GIF")
        self.assertEqual(self.verify(buffer.getvalue()).status_code, 415)

    def test_individual_engines_and_auth_contract(self):
        for endpoint, fields in [
            ("/v1/faces/compare", {"reference": png(), "selfie": png()}),
            ("/v1/liveness", {"selfie": png()}),
            ("/v1/documents/analyze", {"document": png()}),
        ]:
            response = self.client.post(endpoint, files=fields)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["outcome"], "inconclusive")
        self.assertEqual(self.client.post("/v1/auth/authenticate").status_code, 501)


if __name__ == "__main__":
    unittest.main()
