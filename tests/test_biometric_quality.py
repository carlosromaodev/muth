"""Adversarial contracts and quality gates, independent of local accuracy claims."""

import importlib.util
import threading
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from muth.engines.biometric import (
    BiometricManifest,
    Calibration,
    CaptureError,
    FaceCore,
    LivenessCore,
    MiniFASEngine,
    SFaceEngine,
    minifas_crop,
    model_fingerprint,
)
from muth.media import ImageInput

ASSET = {"path": "not-loaded.onnx", "sha256": "0" * 64}
MANIFEST = {
    "schema_version": "biometrics-v1",
    "permitted_use": "research",
    "license_evidence_file": "not-loaded.txt",
    **{name: ASSET for name in ("yunet", "sface", "minifas_v2", "minifas_v1se")},
}
HAS_EXTRAS = all(importlib.util.find_spec(name) for name in ("cv2", "numpy"))


class QualityConfigurationTests(unittest.TestCase):
    def test_defaults_and_roundtrip_have_identical_fingerprint(self):
        manifest = BiometricManifest.model_validate(MANIFEST)
        roundtrip = BiometricManifest.model_validate_json(manifest.model_dump_json())
        for role in ("face", "liveness"):
            self.assertEqual(model_fingerprint(manifest, role), model_fingerprint(roundtrip, role))
            changed = manifest.model_copy(update={"max_nose_offset_ratio": 0.5})
            self.assertNotEqual(model_fingerprint(manifest, role), model_fingerprint(changed, role))
        with self.assertRaises(ValueError):
            model_fingerprint(manifest, "ocr")

    def test_invalid_calibration_does_not_reach_inference(self):
        for threshold in (float("nan"), float("inf"), -1.1, 1.1):
            with self.subTest(threshold=threshold), self.assertRaises(ValueError):
                Calibration(threshold)
        with self.assertRaises(ValueError):
            Calibration(0.5, version="")
        with self.assertRaises(ValueError):
            MiniFASEngine(None, "not-loaded", Calibration(-0.1))

    def test_runtime_upgrade_invalidates_calibration_fingerprint(self):
        manifest = BiometricManifest.model_validate(MANIFEST)
        with patch("muth.engines.biometric.version", return_value="1.0.0"):
            old = model_fingerprint(manifest, "face")
        with patch("muth.engines.biometric.version", return_value="2.0.0"):
            upgraded = model_fingerprint(manifest, "face")
        self.assertNotEqual(old, upgraded)


@unittest.skipUnless(HAS_EXTRAS, "Install the biometrics extra for quality contracts")
class FaceQualityTests(unittest.TestCase):
    def setUp(self):
        import cv2
        import numpy as np

        self.np = np
        self.core = FaceCore.__new__(FaceCore)
        self.core.cv = cv2
        self.core.manifest = BiometricManifest.model_validate(MANIFEST)
        self.core.lock = threading.RLock()
        self.face = np.array(
            [30, 20, 140, 180, 70, 90, 150, 90, 110, 125, 80, 160, 140, 160, 0.99],
            dtype=np.float32,
        )
        self.output = self.face[None].copy()
        self.core.detector = SimpleNamespace(
            setInputSize=lambda _: None, detect=lambda _: (None, self.output)
        )
        frame = np.random.default_rng(918).integers(50, 200, (240, 240, 3), dtype=np.uint8)
        buffer = BytesIO()
        Image.fromarray(frame).save(buffer, format="PNG")
        self.image = ImageInput(buffer.getvalue(), 240, 240, "PNG")

    def assert_capture(self, reason):
        with self.assertRaises(CaptureError) as caught:
            self.core.locate(self.image)
        self.assertEqual(caught.exception.reason, reason)

    def test_valid_geometry_is_supported_without_claiming_pose_angles(self):
        _, _, diagnostics = self.core.locate(self.image)
        self.assertEqual(diagnostics["nose_offset_ratio"], 0)
        self.assertGreater(diagnostics["mouth_height_ratio"], diagnostics["nose_height_ratio"])

    def test_detector_nan_shape_and_confidence_cannot_enter_alignment(self):
        for output in (
            self.np.full((1, 15), float("nan"), dtype=self.np.float32),
            self.np.zeros((1, 14), dtype=self.np.float32),
            self.np.zeros((15,), dtype=self.np.float32),
            self.face[None].copy(),
        ):
            if output.shape == (1, 15) and self.np.isfinite(output).all():
                output[0, 14] = 1.1
            self.output = output
            self.assert_capture("invalid_detector_output")

    def test_reversed_or_outside_landmarks_cannot_enter_alignment(self):
        for index, value in ((8, 241), (9, 80), (13, 80)):
            self.output = self.face[None].copy()
            self.output[0, index] = value
            self.assert_capture("invalid_face_landmarks")

    def test_horizontal_nose_offset_abstains(self):
        self.output[0, 8] = 175
        self.assert_capture("face_pose_unsupported")

    def test_upside_down_eye_order_cannot_pass_roll_reduction(self):
        self.output[0, [4, 5, 6, 7]] = self.face[[6, 7, 4, 5]]
        self.assert_capture("face_pose_unsupported")

    def test_small_detector_face_is_rejected_even_with_large_source(self):
        self.core.image = lambda _: self.np.zeros((3000, 3000, 3), dtype=self.np.uint8)
        self.output[0, :4] = [100, 100, 25, 25]
        self.assert_capture("face_too_small")

    def test_extreme_exposure_abstains(self):
        for value in (0, 255):
            self.core.image = lambda _, value=value: self.np.full(
                (240, 240, 3), value, dtype=self.np.uint8
            )
            self.assert_capture("face_exposure_unsupported")

    def test_invalid_bytes_and_dimension_metadata_produce_abstention(self):
        for image in (
            ImageInput(b"invalid", 240, 240, "PNG"),
            ImageInput(self.image.content, 1, 1, "PNG"),
        ):
            result = SFaceEngine(self.core, "not-loaded", Calibration(0.5)).compare(image, image)
            self.assertIsNone(result.score)
            self.assertIn("invalid_capture_image", result.reasons)

    def test_hidden_alpha_content_is_rejected_and_opaque_alpha_is_supported(self):
        for alpha in (0, 255):
            buffer = BytesIO()
            Image.new("RGBA", (240, 240), (200, 100, 50, alpha)).save(buffer, format="PNG")
            image = ImageInput(buffer.getvalue(), 240, 240, "PNG")
            if alpha == 0:
                with self.assertRaises(CaptureError) as caught:
                    self.core.image(image)
                self.assertEqual(caught.exception.reason, "transparent_capture_unsupported")
            else:
                self.assertEqual(self.core.image(image).shape, (240, 240, 3))

    def test_exif_orientation_applies_after_raw_dimension_validation(self):
        buffer = BytesIO()
        source = Image.new("RGB", (40, 20), "white")
        exif = Image.Exif()
        exif[274] = 6
        source.save(buffer, format="JPEG", exif=exif)
        image = ImageInput(buffer.getvalue(), 40, 20, "JPEG")
        self.assertEqual(self.core.image(image).shape, (40, 20, 3))

    def test_models_are_loaded_from_verified_buffers_without_reopening_paths(self):
        arguments = []

        def detector(*args):
            arguments.append(args)
            return SimpleNamespace()

        def recognizer(*args):
            arguments.append(args)
            return SimpleNamespace(feature=lambda _: self.np.ones((1, 128), self.np.float32))

        fake_cv = SimpleNamespace(
            dnn=SimpleNamespace(DNN_BACKEND_OPENCV=3, DNN_TARGET_CPU=0),
            FaceDetectorYN=SimpleNamespace(create=detector),
            FaceRecognizerSF=SimpleNamespace(create=recognizer),
        )
        data = {"yunet": b"verified-detector-bytes", "sface": b"verified-recognizer-bytes"}
        with patch.dict("sys.modules", {"cv2": fake_cv}):
            FaceCore(self.core.manifest, data)
        self.assertEqual(arguments[0][0], "onnx")
        self.assertEqual(arguments[0][1].tobytes(), data["yunet"])
        self.assertEqual(arguments[1][1].tobytes(), data["sface"])
        self.assertEqual(arguments[0][-2:], (3, 0))
        self.assertEqual(arguments[1][-2:], (3, 0))

    def test_embedding_and_alignment_contracts_cannot_flatten_invalid_shapes(self):
        valid_alignment = self.np.zeros((112, 112, 3), dtype=self.np.uint8)
        for aligned, output, reason in (
            (valid_alignment, self.np.ones((2, 64), self.np.float32), "invalid_face_embedding"),
            (valid_alignment, self.np.zeros((1, 128), self.np.float32), "invalid_face_embedding"),
            (self.np.zeros((1, 1, 3), self.np.uint8), None, "invalid_face_alignment"),
        ):
            self.core.recognizer = SimpleNamespace(
                alignCrop=lambda _, __, aligned=aligned: aligned,
                feature=lambda _, output=output: output,
            )
            with self.assertRaises(CaptureError) as caught:
                self.core.embedding(self.image)
            self.assertEqual(caught.exception.reason, reason)

    def test_large_finite_embedding_cannot_overflow_to_a_zero_vector(self):
        huge = self.np.finfo(self.np.float32).max
        self.core.recognizer = SimpleNamespace(
            alignCrop=lambda *_: self.np.zeros((112, 112, 3), self.np.uint8),
            feature=lambda _: self.np.full((1, 128), huge, self.np.float32),
        )
        with self.np.errstate(over="raise", invalid="raise"):
            vector, _ = self.core.embedding(self.image)
        self.assertAlmostEqual(float(self.np.linalg.norm(vector)), 1, places=6)

    def test_opencv_native_error_abstains_without_exposing_exception_text(self):
        def fail(_):
            raise self.core.cv.error("private internal graph details")

        self.core.detector.detect = fail
        result = SFaceEngine(self.core, "not-loaded", Calibration(0.5)).compare(
            self.image, self.image
        )
        self.assertEqual(result.reasons, ["face_inference_failed"])


@unittest.skipUnless(HAS_EXTRAS, "Install the biometrics extra for inference contracts")
class LivenessContractTests(unittest.TestCase):
    def setUp(self):
        import cv2
        import numpy as np

        self.np, self.cv = np, cv2
        self.frame = np.full((100, 100, 3), [17, 99, 231], dtype=np.uint8)
        self.face = np.array([25, 25, 50, 50], dtype=np.float32)

    def test_crop_validation_preserves_valid_bgr_bytes(self):
        tensor = minifas_crop(self.frame, self.face, 2.7, self.cv)
        self.np.testing.assert_array_equal(tensor[0, :, 40, 40], [17, 99, 231])
        for frame, face, scale in (
            (self.frame.astype(self.np.float32), self.face, 2.7),
            (self.frame, [25, 25, 0, 50], 2.7),
            (self.frame, self.face, float("nan")),
            (self.frame[:, :, 0], self.face, 2.7),
        ):
            with self.assertRaises(CaptureError):
                minifas_crop(frame, face, scale, self.cv)

    def session(self, output, *, input_type="tensor(float)", output_shape=None):
        return SimpleNamespace(
            get_inputs=lambda: [
                SimpleNamespace(name="input", type=input_type, shape=[1, 3, 80, 80])
            ],
            get_outputs=lambda: [
                SimpleNamespace(type="tensor(float)", shape=output_shape or [1, 3])
            ],
            get_providers=lambda: ["CPUExecutionProvider"],
            run=lambda *_: [output],
        )

    def test_startup_rejects_incompatible_onnx_types_and_outputs(self):
        for session in (
            self.session(self.np.zeros((1, 3), self.np.float32), input_type="tensor(double)"),
            self.session(self.np.zeros((1, 3), self.np.float32), output_shape=[1, 2]),
            self.session(self.np.full((1, 3), float("nan"), self.np.float32)),
        ):
            fake_ort = SimpleNamespace(
                set_default_logger_severity=lambda _: None,
                disable_telemetry_events=lambda: None,
                SessionOptions=SimpleNamespace,
                ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL=0),
                InferenceSession=lambda *_args, session=session, **_kwargs: session,
            )
            with (
                patch.dict("sys.modules", {"onnxruntime": fake_ort}),
                self.assertRaises(ValueError),
            ):
                LivenessCore(None, {"minifas_v2": b"", "minifas_v1se": b""}, 1)

    def test_extreme_finite_logits_remain_finite(self):
        core = LivenessCore.__new__(LivenessCore)
        core.face = SimpleNamespace(cv=self.cv, locate=lambda _: (self.frame, self.face, {}))
        huge = self.np.finfo(self.np.float32).max
        core.sessions = [
            self.session(self.np.array([[-huge, huge, -huge]], self.np.float32)),
            self.session(self.np.array([[-huge, huge, -huge]], self.np.float32)),
        ]
        with self.np.errstate(over="raise", invalid="raise"):
            score, _ = core.score(None)
        self.assertEqual(score, 1)

    def test_nan_output_cannot_become_a_liveness_decision(self):
        core = LivenessCore.__new__(LivenessCore)
        core.face = SimpleNamespace(cv=self.cv, locate=lambda _: (self.frame, self.face, {}))
        core.sessions = [self.session(self.np.full((1, 3), float("nan"), self.np.float32))]
        check = MiniFASEngine(core, "not-loaded", Calibration(0.8, "test-policy", True)).assess(
            None
        )
        self.assertIsNone(check.score)
        self.assertEqual(check.outcome, "inconclusive")
        self.assertIn("invalid_liveness_output", check.reasons)

    def test_native_ort_failure_abstains_without_masking_programming_errors(self):
        core = LivenessCore.__new__(LivenessCore)
        core.face = SimpleNamespace(cv=self.cv, locate=lambda _: (self.frame, self.face, {}))
        error = type("Fail", (Exception,), {"__module__": "onnxruntime.capi.test"})

        def fail(*_):
            raise error("private graph details")

        session = self.session(self.np.zeros((1, 3), self.np.float32))
        session.run = fail
        core.sessions = [session]
        check = MiniFASEngine(core, "not-loaded", Calibration(0.8)).assess(None)
        self.assertIn("liveness_inference_failed", check.reasons)
        self.assertIsNone(check.score)
        error = RuntimeError
        with self.assertRaises(RuntimeError):
            core.score(None)
