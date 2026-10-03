import contextlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from muth.cli import main
from muth.diagnostics import inspect_setup
from muth.main import create_app
from tests.test_sessions import settings


class SetupDiagnosticsTests(unittest.TestCase):
    def test_demo_and_missing_languages_are_actionable_without_secret_output(self):
        config = settings(biometric_manifest="private-model-location/manifest.json")
        ocr = SimpleNamespace(executable="private-tesseract-location", languages="eng")
        with (
            patch("muth.diagnostics.TesseractDocumentEngine", return_value=ocr),
            patch("muth.diagnostics.load_manifest", side_effect=ValueError("private detail")),
            patch("muth.diagnostics.find_spec", return_value=object()),
        ):
            report = inspect_setup(config)
        self.assertEqual(report["status"], "setup_required")
        self.assertEqual(report["missing_ocr_languages"], ["por"])
        self.assertTrue(any("reinicia" in action for action in report["actions"]))
        for sensitive in ("private detail", "private-model-location", "private-tesseract-location"):
            self.assertNotIn(sensitive, json.dumps(report))

    def test_prepared_assets_do_not_claim_identity_approval(self):
        config = settings(biometric_manifest="manifest.json").model_copy(
            update={"engine_mode": "biometric"}
        )
        with (
            patch("muth.diagnostics.TesseractDocumentEngine") as engine,
            patch("muth.diagnostics.load_manifest"),
            patch("muth.diagnostics.find_spec", return_value=object()),
        ):
            engine.return_value = SimpleNamespace(executable="tesseract", languages="por+eng")
            report = inspect_setup(config)
        self.assertEqual(report["status"], "ready_for_research")
        self.assertFalse(report["identity_verification_ready"])

    def test_doctor_cli_reports_incomplete_setup_with_nonzero_exit(self):
        report = {"status": "setup_required", "mode": "demo", "actions": ["Configure os motores."]}
        output = io.StringIO()
        with (
            patch("muth.cli.Settings", return_value=settings()),
            patch("muth.diagnostics.inspect_setup", return_value=report),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["doctor"]), 1)
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_public_config_exposes_readiness_and_demo_mode(self):
        app = create_app(settings(document_ocr_enabled=False))
        with TestClient(app) as client:
            result = client.get("/capture-api/config").json()
        self.assertEqual(result["mode"], "demo")
        self.assertFalse(result["biometric_inference_ready"])
        self.assertFalse(result["document_ocr_ready"])
        self.assertTrue(result["setup_required"])
        self.assertNotIn("data_key", result)
