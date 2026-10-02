import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from muth.cli import bootstrap, main
from muth.config import Settings
from muth.engines.bundle import EngineBundle
from muth.evaluation import evaluate_face, validate_manifest, wilson
from muth.main import create_app
from tests.test_muth import checks, png
from tests.test_sessions import PAYLOAD, settings


class OperationTests(unittest.TestCase):
    def test_openapi_declares_api_key_security(self):
        app = create_app(settings())
        self.addCleanup(app.state.database.engine.dispose)
        schema = app.openapi()
        security = schema["components"]["securitySchemes"]["APIKeyHeader"]
        self.assertEqual(security["name"], "X-API-Key")
        self.assertEqual(security["in"], "header")
        self.assertTrue(schema["paths"]["/v1/sessions"]["post"]["security"])

    def test_a_key_cannot_resolve_to_two_tenants(self):
        from pydantic import SecretStr

        with self.assertRaises(ValueError):
            settings(api_key=SecretStr("alpha-token"))

    def test_body_limit_precedes_json_and_multipart_parsing(self):
        with TestClient(
            create_app(settings(max_request_bytes=64)), headers={"X-API-Key": "alpha-token"}
        ) as client:
            for route, headers in [
                ("/v1/sessions", {"Content-Type": "application/json"}),
                ("/v1/verifications", {"Content-Type": "multipart/form-data; boundary=test"}),
            ]:
                response = client.post(route, content=b"x" * 100, headers=headers)
                self.assertEqual(response.status_code, 413)
                self.assertEqual(response.json()["error"]["code"], "request_too_large")
            response = client.post(
                "/v1/sessions",
                content=iter([b"x" * 40, b"y" * 40]),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 413)

    def test_authentication_precedes_body_consumption(self):
        with TestClient(create_app(settings(max_request_bytes=1))) as client:
            response = client.post("/v1/sessions", content=b"private" * 100)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("private", response.text)

    def test_rate_limit_is_per_key(self):
        with TestClient(
            create_app(settings(rate_limit_per_minute=2)), headers={"X-API-Key": "alpha-token"}
        ) as client:
            self.assertEqual(client.post("/v1/sessions", json=PAYLOAD).status_code, 201)
            self.assertEqual(client.post("/v1/sessions", json=PAYLOAD).status_code, 201)
            self.assertEqual(client.post("/v1/sessions", json=PAYLOAD).status_code, 429)
            self.assertEqual(
                client.post(
                    "/v1/sessions", json=PAYLOAD, headers={"X-API-Key": "beta-token"}
                ).status_code,
                201,
            )

    def test_injected_engines_used_by_routes_and_demo_remains_review(self):
        from unittest.mock import Mock

        from muth.domain import Outcome
        from muth.engines.id import DocumentAnalysis
        from muth.media import decode_image

        evidence = checks((Outcome.PASS,) * 3)
        evidence.face_match.provider = "injected-test"
        face, liveness, document = Mock(), Mock(), Mock()
        face.compare.return_value = evidence.face_match
        liveness.assess.return_value = evidence.liveness
        document.analyze.return_value = DocumentAnalysis(
            evidence.document, decode_image(png(), settings())
        )
        app = create_app(settings(), EngineBundle(face, liveness, document))
        with TestClient(app, headers={"X-API-Key": "alpha-token"}) as client:
            response = client.post("/v1/verifications", files={"document": png(), "selfie": png()})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "review")
            self.assertEqual(response.json()["checks"]["face_match"]["provider"], "injected-test")
            response = client.post("/v1/faces/compare", files={"reference": png(), "selfie": png()})
            self.assertEqual(response.json()["provider"], "injected-test")
            self.assertFalse(client.get("/health/ready").json()["identity_verification_ready"])

    def test_bootstrap_is_private_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / ".env"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                bootstrap(target, "local")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            config = Settings(_env_file=target)
            self.assertEqual(config.tenants[0].tenant_id, "local")
            self.assertNotIn(config.data_key.get_secret_value(), output.getvalue())
            original = target.read_bytes()
            with self.assertRaises(FileExistsError):
                bootstrap(target, "local")
            self.assertEqual(target.read_bytes(), original)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def manifest(self):
        (self.root / "weights.onnx").write_bytes(b"synthetic-weight-for-integrity-test")
        (self.root / "review.txt").write_text("Synthetic test licence review; not real weights.")
        content = {
            "model_id": "test",
            "version": "1",
            "role": "face",
            "permitted_use": "research",
            "license_reference": "test-only",
            "license_evidence_file": "review.txt",
            "assets": [
                {
                    "path": "weights.onnx",
                    "sha256": hashlib.sha256((self.root / "weights.onnx").read_bytes()).hexdigest(),
                }
            ],
        }
        path = self.root / "manifest.json"
        path.write_text(json.dumps(content))
        return path

    def test_manifest_integrity_and_license_gates(self):
        path = self.manifest()
        result = validate_manifest(path)
        self.assertTrue(result["integrity_valid"])
        self.assertFalse(result["legal_approval"])
        with self.assertRaises(ValueError):
            validate_manifest(path, require_commercial=True)
        (self.root / "weights.onnx").write_bytes(b"modified")
        with self.assertRaises(ValueError):
            validate_manifest(path)

    def test_missing_license_and_path_traversal_are_rejected(self):
        path = self.manifest()
        (self.root / "review.txt").write_text("")
        with self.assertRaises(ValueError):
            validate_manifest(path)
        (self.root / "review.txt").write_text("review")
        content = json.loads(path.read_text())
        content["assets"][0]["path"] = "../outside.onnx"
        path.write_text(json.dumps(content))
        with self.assertRaises(ValueError):
            validate_manifest(path)

    def test_face_rates_and_confidence_intervals(self):
        path = Path(__file__).parents[1] / "examples/face-pairs.synthetic.csv"
        result = evaluate_face(path, 0.5)
        self.assertEqual(result["test_rows"], 4)
        self.assertEqual(result["fmr"]["rate"], 0.5)
        self.assertEqual(result["fnmr"]["rate"], 0.5)
        self.assertEqual(result["latency_ms"]["p50"], 25)
        self.assertAlmostEqual(result["latency_ms"]["p95"], 38.5)
        self.assertTrue(result["sample_warning"])
        bounds = wilson(0, 100)
        self.assertGreater(bounds["ci95"][1], 0)
        self.assertIsNone(wilson(0, 0)["rate"])

    def test_invalid_scores_and_empty_test_data_rejected(self):
        path = self.root / "pairs.csv"
        for row in ["test,genuine,nan,1", "test,genuine,0.5,-1", "calibration,genuine,0.9,1"]:
            path.write_text("split,label,score,latency_ms\n" + row + "\n")
            with self.assertRaises(ValueError):
                evaluate_face(path, 0.5)
        with self.assertRaises(ValueError):
            evaluate_face(path, float("nan"))

    def test_cli_evaluation_writes_a_reproducible_report(self):
        example = Path(__file__).parents[1] / "examples/face-pairs.synthetic.csv"
        output = self.root / "report.json"
        with contextlib.redirect_stdout(io.StringIO()):
            result = main(
                ["evaluate-face", str(example), "--threshold", "0.5", "--output", str(output)]
            )
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.read_text())["test_rows"], 4)
