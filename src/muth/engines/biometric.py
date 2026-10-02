"""Local YuNet/SFace and MiniFASNet inference; no runtime downloads or pickle loads."""

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from muth.domain import Check, DocumentCheck, Outcome
from muth.engines.bundle import EngineBundle
from muth.engines.id import DocumentAnalysis
from muth.evaluation import Asset, _local_path
from muth.media import ImageInput

PREPROCESSING_VERSION = "yunet-sface-bgr-byte-minifas-2.7-4-v1"


class BiometricManifest(BaseModel):
    model_config = {"extra": "forbid"}
    schema_version: Literal["biometrics-v1"]
    permitted_use: Literal["research", "commercial"]
    license_evidence_file: str
    yunet: Asset
    sface: Asset
    minifas_v2: Asset
    minifas_v1se: Asset
    detector_score_threshold: float = Field(default=0.9, ge=0.5, le=1)
    max_detection_dimension: int = Field(default=960, ge=320, le=1920)
    min_face_pixels: int = Field(default=60, ge=20, le=300)
    min_sharpness: float = Field(default=15, ge=0, le=1000)
    max_roll_degrees: float = Field(default=30, gt=0, le=45)
    face_default_threshold: float = Field(default=0.363, ge=-1, le=1)
    liveness_default_threshold: float = Field(default=0.8, ge=0, le=1)
    inference_threads: int = Field(default=1, ge=1, le=8)


def load_manifest(path: Path):
    manifest = BiometricManifest.model_validate_json(path.read_text())
    root = path.parent.resolve()
    evidence = _local_path(root, manifest.license_evidence_file)
    if not evidence.read_text().strip():
        raise ValueError("Evidência da licença em falta.")
    paths, data = {}, {}
    for name in ("yunet", "sface", "minifas_v2", "minifas_v1se"):
        asset = getattr(manifest, name)
        paths[name] = _local_path(root, asset.path)
        data[name] = paths[name].read_bytes()
        if hashlib.sha256(data[name]).hexdigest() != asset.sha256:
            raise ValueError(f"Artefacto {name} não corresponde ao checksum.")
    return manifest, paths, data


def model_fingerprint(manifest: BiometricManifest, role: str) -> str:
    names = ["yunet", "sface"] if role == "face" else ["yunet", "minifas_v2", "minifas_v1se"]
    configuration = {
        "preprocessing": PREPROCESSING_VERSION,
        "weights": {name: getattr(manifest, name).sha256 for name in names},
        "quality": {
            key: getattr(manifest, key)
            for key in (
                "detector_score_threshold",
                "max_detection_dimension",
                "min_face_pixels",
                "min_sharpness",
                "max_roll_degrees",
            )
        },
    }
    return hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Calibration:
    threshold: float
    version: str = "uncalibrated-baseline"
    validated: bool = False


class CaptureError(Exception):
    def __init__(self, reason: str, diagnostics=None):
        self.reason = reason
        self.diagnostics = diagnostics or {}


class FaceCore:
    def __init__(self, manifest, paths):
        import cv2

        self.cv = cv2
        self.manifest = manifest
        self.lock = threading.RLock()
        self.detector = cv2.FaceDetectorYN.create(
            str(paths["yunet"]), "", (320, 320), manifest.detector_score_threshold, 0.3, 5000
        )
        self.recognizer = cv2.FaceRecognizerSF.create(str(paths["sface"]), "")

    def image(self, image: ImageInput):
        import numpy as np

        with Image.open(BytesIO(image.content)) as source:
            rgb = np.array(ImageOps.exif_transpose(source).convert("RGB"))
        return self.cv.cvtColor(rgb, self.cv.COLOR_RGB2BGR)

    def locate(self, image: ImageInput):
        import numpy as np

        frame = self.image(image)
        height, width = frame.shape[:2]
        scale = min(1.0, self.manifest.max_detection_dimension / max(width, height))
        detection = self.cv.resize(
            frame, (max(1, round(width * scale)), max(1, round(height * scale)))
        )
        with self.lock:
            self.detector.setInputSize((detection.shape[1], detection.shape[0]))
            _, faces = self.detector.detect(detection)
        if faces is None or len(faces) == 0:
            raise CaptureError("no_face_detected")
        if len(faces) != 1:
            raise CaptureError("multiple_faces_detected", {"face_count": float(len(faces))})
        face = faces[0].copy()
        if len(face) != 15 or not np.isfinite(face).all():
            raise CaptureError("invalid_detector_output")
        sx, sy = width / detection.shape[1], height / detection.shape[0]
        face[[0, 2, 4, 6, 8, 10, 12]] *= sx
        face[[1, 3, 5, 7, 9, 11, 13]] *= sy
        x, y, w, h = [float(v) for v in face[:4]]
        if w <= 0 or h <= 0 or x < -1 or y < -1 or x + w > width + 1 or y + h > height + 1:
            raise CaptureError("face_outside_frame")
        x1, y1, x2, y2 = (
            max(0, int(x)),
            max(0, int(y)),
            min(width, math.ceil(x + w)),
            min(height, math.ceil(y + h)),
        )
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            raise CaptureError("empty_face_crop")
        sharpness = float(
            self.cv.Laplacian(self.cv.cvtColor(crop, self.cv.COLOR_BGR2GRAY), self.cv.CV_64F).var()
        )
        eye_dx, eye_dy = float(face[6] - face[4]), float(face[7] - face[5])
        roll = abs(math.degrees(math.atan2(eye_dy, eye_dx)))
        roll = min(roll, abs(180 - roll))
        diagnostics = {
            "face_width": w,
            "face_height": h,
            "sharpness": sharpness,
            "roll_degrees": roll,
            "detector_score": float(face[14]),
        }
        if min(w, h) < self.manifest.min_face_pixels:
            raise CaptureError("face_too_small", diagnostics)
        if sharpness < self.manifest.min_sharpness:
            raise CaptureError("face_too_blurred", diagnostics)
        if roll > self.manifest.max_roll_degrees or math.hypot(eye_dx, eye_dy) < 10:
            raise CaptureError("face_pose_unsupported", diagnostics)
        return frame, face, diagnostics

    def embedding(self, image: ImageInput):
        import numpy as np

        frame, face, diagnostics = self.locate(image)
        with self.lock:
            aligned = self.recognizer.alignCrop(frame, face)
            vector = self.recognizer.feature(aligned).astype(np.float32).reshape(-1)
        norm = float(np.linalg.norm(vector))
        if vector.size != 128 or not np.isfinite(vector).all() or norm <= 1e-12:
            raise CaptureError("invalid_face_embedding")
        return vector / norm, diagnostics


@dataclass(frozen=True)
class SFaceEngine:
    core: FaceCore
    fingerprint: str
    calibration: Calibration

    def compare(self, reference: ImageInput, selfie: ImageInput) -> Check:
        import numpy as np

        started = time.monotonic()
        diagnostics = {}
        score, outcome, reasons = None, Outcome.INCONCLUSIVE, []
        try:
            a, reference_quality = self.core.embedding(reference)
            b, selfie_quality = self.core.embedding(selfie)
            diagnostics.update({f"reference_{k}": v for k, v in reference_quality.items()})
            diagnostics.update({f"selfie_{k}": v for k, v in selfie_quality.items()})
            score = float(np.clip(np.dot(a, b), -1, 1))
            if self.calibration.validated:
                outcome = Outcome.PASS if score >= self.calibration.threshold else Outcome.FAIL
                reasons = ["face_match" if outcome == Outcome.PASS else "face_mismatch"]
            else:
                reasons = ["face_threshold_not_locally_calibrated"]
        except CaptureError as exc:
            reasons, diagnostics = [exc.reason], exc.diagnostics
        diagnostics["latency_ms"] = (time.monotonic() - started) * 1000
        return Check(
            outcome=outcome,
            reasons=reasons,
            provider="opencv-sface",
            model_version="sface-2021dec",
            score=score,
            score_kind="cosine_similarity",
            model_fingerprint=self.fingerprint,
            threshold=self.calibration.threshold,
            calibration_version=self.calibration.version,
            diagnostics=diagnostics,
        )


def minifas_crop(frame, face, scale, cv):
    """Match upstream crop geometry; keep BGR byte values, without dividing by 255."""
    import numpy as np

    sh, sw = frame.shape[:2]
    x, y, w, h = [float(v) for v in face[:4]]
    scale = min((sh - 1) / h, (sw - 1) / w, scale)
    nw, nh = w * scale, h * scale
    left, top, right, bottom = (
        x + w / 2 - nw / 2,
        y + h / 2 - nh / 2,
        x + w / 2 + nw / 2,
        y + h / 2 + nh / 2,
    )
    if left < 0:
        right -= left
        left = 0
    if top < 0:
        bottom -= top
        top = 0
    if right > sw - 1:
        left -= right - sw + 1
        right = sw - 1
    if bottom > sh - 1:
        top -= bottom - sh + 1
        bottom = sh - 1
    crop = frame[int(top) : int(bottom) + 1, int(left) : int(right) + 1]
    if crop.size == 0:
        raise CaptureError("empty_liveness_crop")
    return np.ascontiguousarray(
        cv.resize(crop, (80, 80)).transpose(2, 0, 1)[None], dtype=np.float32
    )


class LivenessCore:
    def __init__(self, face_core: FaceCore, data, threads):
        import onnxruntime as ort

        ort.set_default_logger_severity(3)
        ort.disable_telemetry_events()
        self.face = face_core
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.sessions = [
            ort.InferenceSession(
                data[name], sess_options=options, providers=["CPUExecutionProvider"]
            )
            for name in ("minifas_v2", "minifas_v1se")
        ]
        for session in self.sessions:
            inputs, outputs = session.get_inputs(), session.get_outputs()
            if len(inputs) != 1 or inputs[0].shape != [1, 3, 80, 80] or len(outputs) != 1:
                raise ValueError("Contrato de inferência MiniFASNet inválido.")

    def score(self, selfie: ImageInput):
        import numpy as np

        frame, face, diagnostics = self.face.locate(selfie)
        probabilities = []
        for scale, session in zip((2.7, 4.0), self.sessions, strict=True):
            tensor = minifas_crop(frame, face, scale, self.face.cv)
            output = np.asarray(session.run(None, {session.get_inputs()[0].name: tensor})[0])
            if output.shape != (1, 3) or not np.isfinite(output).all():
                raise CaptureError("invalid_liveness_output")
            logits = output[0] - np.max(output[0])
            prob = np.exp(logits)
            prob /= prob.sum()
            probabilities.append(float(prob[1]))
        diagnostics.update(
            {
                "live_scale_2_7": probabilities[0],
                "live_scale_4_0": probabilities[1],
                "ensemble_disagreement": abs(probabilities[0] - probabilities[1]),
            }
        )
        return float(sum(probabilities) / 2), diagnostics


@dataclass(frozen=True)
class MiniFASEngine:
    core: LivenessCore
    fingerprint: str
    calibration: Calibration

    def assess(self, selfie: ImageInput) -> Check:
        started = time.monotonic()
        score, diagnostics, outcome = None, {}, Outcome.INCONCLUSIVE
        try:
            score, diagnostics = self.core.score(selfie)
            if self.calibration.validated:
                outcome = Outcome.PASS if score >= self.calibration.threshold else Outcome.FAIL
                reasons = ["rgb_pad_pass" if outcome == Outcome.PASS else "presentation_attack"]
            else:
                reasons = ["liveness_threshold_not_locally_calibrated"]
            if diagnostics["ensemble_disagreement"] > 0.5:
                outcome, reasons = Outcome.INCONCLUSIVE, ["liveness_ensemble_disagreement"]
        except CaptureError as exc:
            reasons, diagnostics = [exc.reason], exc.diagnostics
        diagnostics["latency_ms"] = (time.monotonic() - started) * 1000
        return Check(
            outcome=outcome,
            reasons=[*reasons, "rgb_pad_does_not_prove_capture_freshness"],
            provider="minifasnet-onnx",
            model_version="minifas-v2-v1se",
            model_fingerprint=self.fingerprint,
            score=score,
            score_kind="liveness_softmax",
            threshold=self.calibration.threshold,
            calibration_version=self.calibration.version,
            diagnostics=diagnostics,
        )


class PortraitOnlyDocumentEngine:
    """Locate a portrait to exercise biometrics; no OCR or authenticity claim."""

    def __init__(self, core: FaceCore):
        self.core = core

    def analyze(self, document: ImageInput) -> DocumentAnalysis:
        portrait, reasons = (
            None,
            ["document_authenticity_not_verified", "ocr_outside_biometric_scope"],
        )
        try:
            frame, face, _ = self.core.locate(document)
            x, y, w, h = [float(v) for v in face[:4]]
            height, width = frame.shape[:2]
            left, top = max(0, int(x - w * 0.3)), max(0, int(y - h * 0.3))
            right, bottom = min(width, math.ceil(x + w * 1.3)), min(height, math.ceil(y + h * 1.3))
            crop = frame[top:bottom, left:right]
            ok, encoded = self.core.cv.imencode(".png", crop)
            if not ok:
                raise CaptureError("portrait_encoding_failed")
            portrait = ImageInput(encoded.tobytes(), crop.shape[1], crop.shape[0], "PNG")
        except CaptureError as exc:
            reasons.append(exc.reason)
        return DocumentAnalysis(
            DocumentCheck(
                outcome=Outcome.INCONCLUSIVE,
                reasons=reasons,
                provider="muth-portrait-only",
                model_version="portrait-v1",
            ),
            portrait,
        )


class BiometricRuntime:
    def __init__(self, path: Path):
        manifest, paths, data = load_manifest(path)
        self.manifest = manifest
        core = FaceCore(manifest, paths)
        self.face = SFaceEngine(
            core, model_fingerprint(manifest, "face"), Calibration(manifest.face_default_threshold)
        )
        self.liveness = MiniFASEngine(
            LivenessCore(core, data, manifest.inference_threads),
            model_fingerprint(manifest, "liveness"),
            Calibration(manifest.liveness_default_threshold),
        )
        self.document = PortraitOnlyDocumentEngine(core)

    def bundle(self, face: Calibration | None = None, liveness: Calibration | None = None):
        return EngineBundle(
            replace(self.face, calibration=face or self.face.calibration),
            replace(self.liveness, calibration=liveness or self.liveness.calibration),
            self.document,
        )
