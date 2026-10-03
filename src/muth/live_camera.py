"""Transient camera framing guidance, without identity or authenticity decisions.

The document descriptor helps the browser avoid capturing the same side twice;
it is never stored and cannot establish that a different image is the verso.
"""

import math
from io import BytesIO
from typing import Literal

from PIL import Image
from pydantic import BaseModel, Field

from muth.document_image import geometry_available, prepare_document
from muth.engines.biometric import CaptureError
from muth.media import ImageInput
from muth.services.capture import assess_document_quality

CameraTarget = Literal["document_front", "document_back", "selfie"]


class CameraBounds(BaseModel):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    width: float = Field(gt=0, le=1, allow_inf_nan=False)
    height: float = Field(gt=0, le=1, allow_inf_nan=False)


class CameraPoint(BaseModel):
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)


class CameraAssessment(BaseModel):
    version: Literal["camera-assessment-v1"] = "camera-assessment-v1"
    target: CameraTarget
    detector_ready: bool
    detected: bool = False
    ready: bool = False
    kind: Literal["card", "face"] | None = None
    bounds: CameraBounds | None = None
    corners: list[CameraPoint] | None = Field(default=None, min_length=4, max_length=4)
    reasons: list[str]
    appearance_signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    authenticity_confirmed: Literal[False] = False


def _framing_reasons(bounds: CameraBounds, kind: str) -> list[str]:
    reasons = []
    if min(bounds.x, bounds.y, 1 - bounds.x - bounds.width, 1 - bounds.y - bounds.height) < 0.025:
        reasons.append(f"{kind}_clipped")
    if (
        abs(bounds.x + bounds.width / 2 - 0.5) > 0.22
        or abs(bounds.y + bounds.height / 2 - 0.5) > 0.22
    ):
        reasons.append(f"{kind}_off_center")
    return reasons


def _appearance(image: Image.Image) -> str:
    gray = image.convert("L").resize((9, 8), Image.Resampling.BILINEAR)
    pixels = gray.tobytes()
    value = 0
    for row in range(8):
        for column in range(8):
            value = (value << 1) | (pixels[row * 9 + column] > pixels[row * 9 + column + 1])
    return f"{value:016x}"


def assess_camera(image: ImageInput, target: CameraTarget, *, runtime=None) -> CameraAssessment:
    if target == "selfie":
        return _assess_face(image, runtime)
    result = CameraAssessment(target=target, detector_ready=geometry_available(), reasons=[])
    if not result.detector_ready:
        result.reasons = ["document_detector_unavailable"]
        return result
    # Transparency must not become an apparently well lit card after compositing.
    with Image.open(BytesIO(image.content)) as source:
        if ("A" in source.getbands() or "transparency" in source.info) and source.convert(
            "RGBA"
        ).getchannel("A").getextrema()[0] < 255:
            result.reasons = ["document_transparency_unsupported"]
            return result
    region = prepare_document(image)
    if not region.source_isolated or len(region.corners) != 4:
        result.reasons = ["document_not_detected"]
        return result
    bounds = region.normalized_bounds
    if not _valid_geometry(bounds, region.corners):
        result.reasons = ["document_not_detected"]
        return result
    result.detected, result.kind = True, "card"
    result.bounds = CameraBounds(**bounds)
    result.corners = [CameraPoint(x=x, y=y) for x, y in region.corners]
    result.appearance_signature = _appearance(region.image)
    short, long = sorted(region.native_size)
    if short < 240 or long < 380 or result.bounds.width * result.bounds.height < 0.14:
        result.reasons.append("document_too_small")
    if not 1.15 <= long / max(short, 1) <= 2.30:
        result.reasons.append("document_shape_unsupported")
    quality = assess_document_quality(region.as_image_input(), "preview")
    if quality.diagnostics["edge_variance"] < 20:
        result.reasons.append("document_too_blurred")
    if (
        max(
            quality.diagnostics["dark_clipped_fraction"],
            quality.diagnostics["bright_clipped_fraction"],
        )
        > 0.95
    ):
        result.reasons.append("document_exposure_unsupported")
    result.reasons.extend(_framing_reasons(result.bounds, "document"))
    result.ready = not result.reasons
    return result


def _valid_geometry(bounds, corners) -> bool:
    """Reject inconsistent detector output before constructing the response contract."""
    try:
        x, y, width, height = (bounds[key] for key in ("x", "y", "width", "height"))
        if (
            not all(math.isfinite(value) for value in (x, y, width, height))
            or not 0 <= x < 1
            or not 0 <= y < 1
            or not 0 < width <= 1 - x
            or not 0 < height <= 1 - y
            or len(corners) != 4
        ):
            return False
        if any(
            len(point) != 2
            or any(not math.isfinite(value) or not 0 <= value <= 1 for value in point)
            for point in corners
        ):
            return False
        twice_area = sum(
            corners[index][0] * corners[(index + 1) % 4][1]
            - corners[(index + 1) % 4][0] * corners[index][1]
            for index in range(4)
        )
        return abs(twice_area) > 1e-6
    except (KeyError, TypeError, ValueError):
        return False


def _assess_face(image: ImageInput, runtime) -> CameraAssessment:
    result = CameraAssessment(target="selfie", detector_ready=runtime is not None, reasons=[])
    if runtime is None:
        result.reasons = ["face_detector_unavailable"]
        return result
    try:
        frame, face, _ = runtime.face.core.locate(image)
    except CaptureError as exc:
        result.reasons = [exc.reason]
        return result
    height, width = frame.shape[:2]
    x, y, w, h = map(float, face[:4])
    if (
        not all(math.isfinite(value) for value in (x, y, w, h))
        or width <= 0
        or height <= 0
        or w <= 0
        or h <= 0
        or x < -1
        or y < -1
        or x + w > width + 1
        or y + h > height + 1
        or x >= width
        or y >= height
    ):
        result.reasons = ["invalid_detector_output"]
        return result
    result.detected, result.kind = True, "face"
    x1, y1, x2, y2 = max(0.0, x), max(0.0, y), min(float(width), x + w), min(float(height), y + h)
    if x2 <= x1 or y2 <= y1:
        result.detected, result.kind = False, None
        result.reasons = ["invalid_detector_output"]
        return result
    result.bounds = CameraBounds(
        x=x1 / width, y=y1 / height, width=(x2 - x1) / width, height=(y2 - y1) / height
    )
    if result.bounds.width * result.bounds.height < 0.065:
        result.reasons.append("face_too_small")
    if result.bounds.width * result.bounds.height > 0.70:
        result.reasons.append("face_too_close")
    result.reasons.extend(_framing_reasons(result.bounds, "face"))
    result.ready = not result.reasons
    return result
