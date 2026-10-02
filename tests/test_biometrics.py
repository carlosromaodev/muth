import hashlib
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image, ImageFilter, ImageOps

from muth.config import Settings
from muth.engines.biometric import BiometricRuntime, load_manifest, minifas_crop
from muth.main import create_app
from muth.media import decode_image
from tests.test_muth import png
from tests.test_sessions import PAYLOAD, settings

ROOT = Path(__file__).parents[1]


class BiometricContractsTests(unittest.TestCase):
    def test_corrupted_bundle_fails_before_loading_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.onnx").write_bytes(b"integrity-test-only")
            (root / "review.txt").write_text("Test evidence only")
            asset = {
                "path": "model.onnx",
                "sha256": hashlib.sha256(b"integrity-test-only").hexdigest(),
            }
            manifest = {
                "schema_version": "biometrics-v1",
                "permitted_use": "research",
                "license_evidence_file": "review.txt",
                **{name: asset for name in ["yunet", "sface", "minifas_v2", "minifas_v1se"]},
            }
            path = root / "manifest.json"
            path.write_text(json.dumps(manifest))
            load_manifest(path)
            (root / "model.onnx").write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                load_manifest(path)

    def test_mode_requires_explicit_bundle(self):
        with self.assertRaises(ValueError):
            create_app(Settings(_env_file=None, engine_mode="biometric"))

    def test_new_api_scopes_and_demo_cannot_label_or_refine(self):
        with TestClient(create_app(settings()), headers={"X-API-Key": "alpha-token"}) as client:
            payload = {**PAYLOAD, "consent": {**PAYLOAD["consent"], "learning_opt_in": True}}
            sid = client.post("/v1/sessions", json=payload).json()["session_id"]
            client.post(
                f"/v1/sessions/{sid}/verify",
                headers={"Idempotency-Key": "attempt-one"},
                files={"document": png(), "selfie": png()},
            )
            feedback = {"role": "face", "label": "genuine", "source_reference": "review-12345"}
            self.assertEqual(
                client.post(f"/v1/sessions/{sid}/feedback", json=feedback).status_code, 409
            )
            for endpoint in ["/v1/learning/refine", f"/v1/sessions/{sid}/feedback"]:
                self.assertEqual(
                    client.post(
                        endpoint, json=feedback, headers={"X-API-Key": "reader-token"}
                    ).status_code,
                    403,
                )
            self.assertEqual(client.post("/v1/learning/refine").json()["status"], "disabled")
            self.assertEqual(client.get("/v1/learning").json()["samples"], {})
            self.assertEqual(
                client.post(
                    f"/v1/sessions/{sid}/feedback",
                    json=feedback,
                    headers={"X-API-Key": "beta-token"},
                ).status_code,
                404,
            )


@unittest.skipUnless(os.environ.get("MUTH_TEST_BIOMETRICS") == "1", "Explicit local model tests")
class CpuInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = BiometricRuntime(ROOT / "models/biometrics/manifest.json")
        cls.config = Settings(_env_file=None)
        cls.sample = (ROOT / "models/biometrics/samples/image_T1.jpg").read_bytes()

    def image(self, content):
        return decode_image(content, self.config)

    def test_self_comparison_and_uncalibrated_abstention(self):
        image = self.image(self.sample)
        check = self.runtime.face.compare(image, image)
        self.assertGreater(check.score, 0.999)
        self.assertEqual(check.outcome, "inconclusive")
        self.assertEqual(check.score_kind, "cosine_similarity")
        self.assertEqual(len(check.model_fingerprint), 64)

    def test_public_liveness_examples_and_cpu_provider(self):
        scores = []
        for name in ["T1", "F1", "F2"]:
            image = self.image((ROOT / f"models/biometrics/samples/image_{name}.jpg").read_bytes())
            check = self.runtime.liveness.assess(image)
            self.assertEqual(check.outcome, "inconclusive")
            scores.append(check.score)
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[1], scores[2])
        for session in self.runtime.liveness.core.sessions:
            self.assertEqual(session.get_providers(), ["CPUExecutionProvider"])

    def test_no_face_and_blur_cannot_pass(self):
        image = self.image(png((300, 300)))
        self.assertIsNone(self.runtime.face.compare(image, image).score)
        with Image.open(BytesIO(self.sample)) as source:
            buffer = BytesIO()
            ImageOps.exif_transpose(source).filter(ImageFilter.GaussianBlur(30)).save(
                buffer, format="PNG"
            )
        check = self.runtime.liveness.assess(self.image(buffer.getvalue()))
        self.assertEqual(check.outcome, "inconclusive")
        self.assertIsNone(check.score)

    def test_multiple_faces_rejected(self):
        with Image.open(BytesIO(self.sample)) as source:
            source = ImageOps.exif_transpose(source)
            combined = Image.new("RGB", (source.width * 2 + 30, source.height), "white")
            combined.paste(source, (0, 0))
            combined.paste(source, (source.width + 30, 0))
            buffer = BytesIO()
            combined.save(buffer, format="PNG")
        check = self.runtime.liveness.assess(self.image(buffer.getvalue()))
        self.assertIn("multiple_faces_detected", check.reasons)

    def test_minifas_preprocessing_preserves_bgr_byte_scale(self):
        import numpy as np

        frame = np.full((100, 100, 3), [17, 99, 231], dtype=np.uint8)
        face = np.array([25, 25, 50, 50], dtype=np.float32)
        result = minifas_crop(frame, face, 2.7, self.runtime.face.core.cv)
        self.assertEqual(result.shape, (1, 3, 80, 80))
        self.assertEqual(result.dtype, np.float32)
        np.testing.assert_array_equal(result[0, :, 40, 40], [17, 99, 231])

    def test_real_api_feedback_replay_and_consent_withdrawal(self):
        config = settings().model_copy(
            update={
                "engine_mode": "biometric",
                "biometric_manifest": str(ROOT / "models/biometrics/manifest.json"),
            }
        )
        with TestClient(create_app(config), headers={"X-API-Key": "alpha-token"}) as client:
            payload = {**PAYLOAD, "consent": {**PAYLOAD["consent"], "learning_opt_in": True}}
            sid = client.post("/v1/sessions", json=payload).json()["session_id"]
            args = {
                "headers": {"Idempotency-Key": "attempt-one"},
                "files": {"document": self.sample, "selfie": self.sample},
            }
            response = client.post(f"/v1/sessions/{sid}/verify", **args)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "review")
            self.assertEqual(
                client.post(f"/v1/sessions/{sid}/verify", **args).json(), response.json()
            )
            feedback = {"role": "liveness", "label": "live", "source_reference": "human-review-123"}
            self.assertEqual(
                client.post(f"/v1/sessions/{sid}/feedback", json=feedback).status_code, 200
            )
            self.assertEqual(
                client.post("/v1/learning/refine").json()["face"]["status"], "collecting"
            )
            self.assertEqual(client.delete(f"/v1/sessions/{sid}/learning-consent").status_code, 204)
            self.assertEqual(client.get("/v1/learning").json()["samples"], {})
            self.assertEqual(client.get(f"/v1/sessions/{sid}").status_code, 200)
