"""Live framing gates use synthetic pixels and never make identity decisions."""

import json
import unittest
from dataclasses import replace
from io import BytesIO
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image, ImageDraw

from muth.document_image import PreparedDocument
from muth.engines.biometric import CaptureError
from muth.live_camera import assess_camera
from muth.media import ImageInput


def encoded(image):
    content = BytesIO()
    image.save(content, format="PNG")
    return ImageInput(content.getvalue(), *image.size, "PNG")


def textured_card(size=(600, 375)):
    image = Image.new("RGB", size, (210, 215, 205))
    draw = ImageDraw.Draw(image)
    for row in range(15, size[1] - 10, 18):
        draw.rectangle((15, row, size[0] - 15, row + 3), fill=(35, 40, 30))
    return image


def prepared_card(*, size=(600, 375), corners=None, image=None):
    return PreparedDocument(
        image=image if image is not None else textured_card(size),
        source_isolated=True,
        strategy="quadrilateral",
        reasons=("document_region_isolated",),
        native_size=size,
        corners=corners or ((0.2, 0.2), (0.8, 0.2), (0.8, 0.65), (0.2, 0.65)),
    )


def face_runtime(box=(250, 120, 300, 360), frame_size=(800, 600)):
    runtime = Mock()
    width, height = frame_size
    runtime.face.core.locate.return_value = (
        np.zeros((height, width, 3), dtype=np.uint8),
        np.array(box, dtype=float),
        {"discarded_provider_diagnostic": "synthetic-only"},
    )
    return runtime


class LiveDocumentDetectorTests(unittest.TestCase):
    def setUp(self):
        self.image = encoded(Image.new("RGB", (1000, 800), (70, 75, 80)))

    def assess(self, region, target="document_front"):
        with (
            patch("muth.live_camera.geometry_available", return_value=True),
            patch("muth.live_camera.prepare_document", return_value=region),
        ):
            return assess_camera(self.image, target)

    def test_available_detector_returns_ready_card_with_four_normalized_corners(self):
        for target in ("document_front", "document_back"):
            with self.subTest(target=target):
                result = self.assess(prepared_card(), target)
                self.assertEqual(result.target, target)
                self.assertTrue(result.detector_ready)
                self.assertTrue(result.detected)
                self.assertTrue(result.ready)
                self.assertEqual(result.kind, "card")
                self.assertEqual(result.reasons, [])
                self.assertEqual(len(result.corners), 4)
                self.assertAlmostEqual(result.bounds.x, 0.2)
                self.assertAlmostEqual(result.bounds.y, 0.2)
                self.assertAlmostEqual(result.bounds.width, 0.6)
                self.assertAlmostEqual(result.bounds.height, 0.45)
                self.assertRegex(result.appearance_signature, r"^[0-9a-f]{16}$")
                self.assertFalse(result.authenticity_confirmed)

    def test_guidance_is_repeatable_and_contains_no_pixels_or_identity_fields(self):
        first = self.assess(prepared_card())
        second = self.assess(prepared_card())
        self.assertEqual(first.appearance_signature, second.appearance_signature)
        payload = first.model_dump(mode="json")
        self.assertEqual(
            set(payload),
            {
                "version",
                "target",
                "detector_ready",
                "detected",
                "ready",
                "kind",
                "bounds",
                "corners",
                "reasons",
                "appearance_signature",
                "authenticity_confirmed",
            },
        )
        self.assertIs(payload["authenticity_confirmed"], False)
        self.assertNotIn("synthetic-only", json.dumps(payload))

    def test_unavailable_detector_abstains_without_preparing_a_document(self):
        with (
            patch("muth.live_camera.geometry_available", return_value=False),
            patch("muth.live_camera.prepare_document") as prepare,
        ):
            result = assess_camera(self.image, "document_front")
        prepare.assert_not_called()
        self.assertFalse(result.detector_ready)
        self.assertFalse(result.ready)
        self.assertFalse(result.detected)
        self.assertEqual(result.reasons, ["document_detector_unavailable"])
        self.assertIsNone(result.appearance_signature)

    def test_full_document_frame_without_visible_corners_does_not_trigger_capture(self):
        retained = replace(
            prepared_card(),
            source_isolated=False,
            corners=(),
            frame_preserved=True,
        )
        result = self.assess(retained)
        self.assertTrue(result.detector_ready)
        self.assertFalse(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.reasons, ["document_not_detected"])
        self.assertIsNone(result.bounds)
        self.assertIsNone(result.corners)

    def test_three_corners_are_insufficient_even_when_a_crop_is_available(self):
        result = self.assess(replace(prepared_card(), corners=prepared_card().corners[:3]))
        self.assertFalse(result.detected)
        self.assertEqual(result.reasons, ["document_not_detected"])

    def test_transparent_source_is_rejected_before_any_geometry_analysis(self):
        transparent = Image.new("RGBA", (1000, 800), (210, 215, 205, 0))
        with (
            patch("muth.live_camera.geometry_available", return_value=True),
            patch("muth.live_camera.prepare_document") as prepare,
        ):
            result = assess_camera(encoded(transparent), "document_back")
        prepare.assert_not_called()
        self.assertFalse(result.ready)
        self.assertFalse(result.detected)
        self.assertEqual(result.reasons, ["document_transparency_unsupported"])
        self.assertIsNone(result.appearance_signature)

    def test_invalid_corner_geometry_abstains_instead_of_raising_or_clamping(self):
        invalid_corners = (
            ((float("nan"), 0.2), (0.8, 0.2), (0.8, 0.65), (0.2, 0.65)),
            ((0.2, float("inf")), (0.8, 0.2), (0.8, 0.65), (0.2, 0.65)),
            ((-0.1, 0.2), (0.8, 0.2), (0.8, 0.65), (-0.1, 0.65)),
            ((0.2, 0.2), (1.1, 0.2), (1.1, 0.65), (0.2, 0.65)),
            ((0.2, 0.2), (0.8, 0.2), (0.8, 1.1), (0.2, 1.1)),
            ((0.2, 0.2), (0.2, 0.2), (0.2, 0.65), (0.2, 0.65)),
            ((0.2, 0.2), (0.8, 0.2), (0.8, 0.2), (0.2, 0.2)),
            ((0.2, 0.2), (0.4, 0.35), (0.6, 0.5), (0.8, 0.65)),
            ((0.2, 0.2), (0.8, 0.65), (0.8, 0.2), (0.2, 0.65)),
        )
        for corners in invalid_corners:
            with self.subTest(corners=corners):
                result = self.assess(replace(prepared_card(), corners=corners))
                self.assertFalse(result.detected)
                self.assertFalse(result.ready)
                self.assertEqual(result.reasons, ["document_not_detected"])
                self.assertIsNone(result.appearance_signature)

    def test_missing_and_malformed_document_bounds_abstain(self):
        invalid_bounds = (
            None,
            {},
            {"x": 0.2, "y": 0.2, "width": 0.6},
            {"x": "invalid", "y": 0.2, "width": 0.6, "height": 0.45},
            {"x": 0.2, "y": (), "width": 0.6, "height": 0.45},
            {"x": 0.2, "y": 0.2, "width": float("nan"), "height": 0.45},
            {"x": 0.8, "y": 0.2, "width": 0.6, "height": 0.45},
        )
        for bounds in invalid_bounds:
            with self.subTest(bounds=bounds):
                region = Mock(
                    source_isolated=True,
                    corners=prepared_card().corners,
                    normalized_bounds=bounds,
                )
                result = self.assess(region)
                self.assertFalse(result.detected)
                self.assertFalse(result.ready)
                self.assertEqual(result.reasons, ["document_not_detected"])
                self.assertIsNone(result.bounds)

    def test_small_native_card_cannot_be_made_ready_by_a_large_output(self):
        region = prepared_card(size=(230, 140), image=textured_card((1200, 750)))
        result = self.assess(region)
        self.assertTrue(result.detected)
        self.assertFalse(result.ready)
        self.assertIn("document_too_small", result.reasons)

    def test_low_frame_occupancy_prevents_capture_of_a_distant_card(self):
        corners = ((0.35, 0.4), (0.65, 0.4), (0.65, 0.6), (0.35, 0.6))
        result = self.assess(prepared_card(corners=corners))
        self.assertTrue(result.detected)
        self.assertEqual(result.reasons, ["document_too_small"])
        self.assertFalse(result.ready)

    def test_card_blur_is_checked_on_the_actual_crop(self):
        result = self.assess(prepared_card(image=Image.new("RGB", (600, 375), (128, 128, 128))))
        self.assertTrue(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.reasons, ["document_too_blurred"])

    def test_severe_under_and_overexposure_block_document_capture(self):
        for color in ("black", "white"):
            with self.subTest(color=color):
                result = self.assess(prepared_card(image=Image.new("RGB", (600, 375), color)))
                self.assertTrue(result.detected)
                self.assertFalse(result.ready)
                self.assertIn("document_exposure_unsupported", result.reasons)

    def test_unsupported_card_aspect_prevents_capture(self):
        for size in ((600, 600), (1000, 300)):
            with self.subTest(size=size):
                result = self.assess(prepared_card(size=size))
                self.assertTrue(result.detected)
                self.assertFalse(result.ready)
                self.assertIn("document_shape_unsupported", result.reasons)

    def test_clipped_card_stays_detected_but_not_ready(self):
        corners = ((0.01, 0.2), (0.6, 0.2), (0.6, 0.65), (0.01, 0.65))
        result = self.assess(prepared_card(corners=corners))
        self.assertTrue(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.reasons, ["document_clipped"])

    def test_off_center_card_requires_repositioning(self):
        corners = ((0.03, 0.2), (0.48, 0.2), (0.48, 0.6), (0.03, 0.6))
        result = self.assess(prepared_card(corners=corners))
        self.assertTrue(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.reasons, ["document_off_center"])


class LiveFaceDetectorTests(unittest.TestCase):
    def setUp(self):
        self.image = encoded(Image.new("RGB", (800, 600), (90, 95, 100)))

    def test_ready_face_only_calls_locate_without_embedding_match_or_liveness(self):
        runtime = face_runtime()
        with patch("muth.live_camera.prepare_document") as prepare:
            result = assess_camera(self.image, "selfie", runtime=runtime)
        runtime.face.core.locate.assert_called_once_with(self.image)
        runtime.face.core.embedding.assert_not_called()
        runtime.face.extract.assert_not_called()
        runtime.face.compare.assert_not_called()
        runtime.liveness.assess.assert_not_called()
        prepare.assert_not_called()
        self.assertTrue(result.detector_ready)
        self.assertTrue(result.detected)
        self.assertTrue(result.ready)
        self.assertEqual(result.kind, "face")
        self.assertEqual(result.reasons, [])
        self.assertAlmostEqual(result.bounds.x, 250 / 800)
        self.assertAlmostEqual(result.bounds.y, 120 / 600)
        self.assertIsNone(result.corners)
        self.assertIsNone(result.appearance_signature)
        self.assertFalse(result.authenticity_confirmed)
        self.assertNotIn("discarded_provider_diagnostic", result.model_dump())

    def test_missing_biometric_runtime_abstains_without_claiming_detection(self):
        result = assess_camera(self.image, "selfie")
        self.assertFalse(result.detector_ready)
        self.assertFalse(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.reasons, ["face_detector_unavailable"])
        self.assertFalse(result.authenticity_confirmed)

    def test_locate_failures_retain_quality_reasons_without_running_biometrics(self):
        for reason in ("face_not_detected", "multiple_faces", "face_blurred", "face_exposure"):
            with self.subTest(reason=reason):
                runtime = face_runtime()
                runtime.face.core.locate.side_effect = CaptureError(reason)
                result = assess_camera(self.image, "selfie", runtime=runtime)
                self.assertTrue(result.detector_ready)
                self.assertFalse(result.detected)
                self.assertFalse(result.ready)
                self.assertEqual(result.reasons, [reason])
                self.assertIsNone(result.bounds)
                runtime.face.core.embedding.assert_not_called()
                runtime.face.compare.assert_not_called()
                runtime.liveness.assess.assert_not_called()

    def test_nonfinite_negative_degenerate_and_out_of_frame_boxes_are_rejected(self):
        invalid_boxes = (
            (float("nan"), 120, 300, 360),
            (250, float("inf"), 300, 360),
            (250, 120, float("inf"), 360),
            (250, 120, 300, float("nan")),
            (-2, 120, 300, 360),
            (250, -2, 300, 360),
            (250, 120, 0, 360),
            (250, 120, 300, -1),
            (501, 120, 301, 360),
            (250, 241, 300, 361),
            (-0.5, 120, 0.1, 360),
            (800.5, 120, 0.1, 360),
            (250, -0.5, 300, 0.1),
            (250, 600.5, 300, 0.1),
        )
        for box in invalid_boxes:
            with self.subTest(box=box):
                result = assess_camera(self.image, "selfie", runtime=face_runtime(box))
                self.assertTrue(result.detector_ready)
                self.assertFalse(result.detected)
                self.assertFalse(result.ready)
                self.assertEqual(result.reasons, ["invalid_detector_output"])
                self.assertIsNone(result.bounds)

    def test_empty_detector_frame_is_rejected(self):
        for size in ((0, 600), (800, 0)):
            with self.subTest(size=size):
                result = assess_camera(self.image, "selfie", runtime=face_runtime(frame_size=size))
                self.assertFalse(result.detected)
                self.assertFalse(result.ready)
                self.assertEqual(result.reasons, ["invalid_detector_output"])

    def test_small_face_blocks_capture_even_when_centered(self):
        result = assess_camera(self.image, "selfie", runtime=face_runtime((360, 250, 80, 100)))
        self.assertTrue(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.reasons, ["face_too_small"])

    def test_face_occupying_most_of_frame_requires_moving_away(self):
        result = assess_camera(self.image, "selfie", runtime=face_runtime((25, 25, 750, 550)))
        self.assertTrue(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.reasons, ["face_too_close"])

    def test_clipped_face_blocks_capture(self):
        result = assess_camera(self.image, "selfie", runtime=face_runtime((10, 150, 600, 300)))
        self.assertTrue(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.reasons, ["face_clipped"])

    def test_off_center_face_requires_repositioning(self):
        result = assess_camera(self.image, "selfie", runtime=face_runtime((30, 150, 150, 300)))
        self.assertTrue(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.reasons, ["face_off_center"])

    def test_tolerated_subpixel_overflow_is_clipped_and_never_ready(self):
        result = assess_camera(self.image, "selfie", runtime=face_runtime((-0.5, 150, 600, 300)))
        self.assertTrue(result.detected)
        self.assertFalse(result.ready)
        self.assertEqual(result.bounds.x, 0)
        self.assertIn("face_clipped", result.reasons)
        self.assertFalse(result.authenticity_confirmed)
