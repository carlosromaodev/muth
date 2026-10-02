import unittest
from io import BytesIO

from PIL import Image, ImageDraw
from pydantic import ValidationError

from muth.capture_models import CaptureScore
from muth.domain import Check, DocumentCheck, Outcome, VerificationStatus
from muth.engines.demo import DemoDocumentEngine, DemoFaceEngine, DemoLivenessEngine
from muth.engines.id import DocumentAnalysis
from muth.media import ImageInput
from muth.services.capture import (
    CaptureService,
    assess_document_quality,
    indicative_score,
)
from muth.services.verify import VerifyService


def document(side=0, size=(1080, 720), *, encoding="PNG"):
    image = Image.new("RGB", size, (219, 222, 218))
    draw = ImageDraw.Draw(image)
    for row in range(20, size[1] - 20, 24):
        draw.rectangle((25 + side, row, size[0] - 35, row + 4), fill=(40, 45, 40))
    draw.rectangle((70 + side * 30, 70, 140 + side * 30, 140), fill=(100, 135, 140))
    buffer = BytesIO()
    image.save(buffer, format=encoding)
    return ImageInput(buffer.getvalue(), *image.size, encoding)


def uniform(color=(128, 128, 128), size=(1080, 720)):
    image = Image.new("RGB", size, color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return ImageInput(buffer.getvalue(), *size, "PNG")


def measurement(role, value, *, outcome=Outcome.INCONCLUSIVE, reasons=None, diagnostics=None):
    return Check(
        outcome=outcome,
        reasons=reasons
        or [
            "face_threshold_not_locally_calibrated"
            if role == "face"
            else "liveness_threshold_not_locally_calibrated"
        ],
        provider=f"test-{role}",
        model_version="test-v1",
        score=value,
        score_kind="cosine_similarity" if role == "face" else "liveness_softmax",
        diagnostics=diagnostics or {},
    )


class StubFace:
    def __init__(self, check):
        self.check = check

    def compare(self, reference, selfie):
        return self.check


class StubLiveness:
    def __init__(self, check):
        self.check = check

    def assess(self, selfie):
        return self.check


class StubDocument:
    def __init__(self, outcome=Outcome.INCONCLUSIVE):
        self.outcome = outcome

    def analyze(self, image):
        return DocumentAnalysis(
            DocumentCheck(
                outcome=self.outcome,
                reasons=["document_authenticity_not_verified"],
                provider="test-document",
                model_version="test-v1",
            ),
            portrait=image,
        )


def service(*, face=None, liveness=None, document_outcome=Outcome.INCONCLUSIVE):
    return CaptureService(
        VerifyService(
            StubFace(face or measurement("face", 0.95)),
            StubLiveness(liveness or measurement("liveness", 0.98)),
            StubDocument(document_outcome),
            demo=False,
        )
    )


class DocumentQualityTests(unittest.TestCase):
    def test_readable_images_have_no_authenticity_outcome_or_document_fields(self):
        quality = assess_document_quality(document(), "front")
        self.assertTrue(quality.readable)
        self.assertGreater(quality.value, 0)
        self.assertLessEqual(quality.value, 1)
        self.assertEqual(quality.reasons, [])
        self.assertGreater(quality.diagnostics["edge_variance"], 20)

    def test_blank_and_clipped_images_cannot_supply_a_rating(self):
        for color in [(255, 255, 255), (0, 0, 0), (128, 128, 128)]:
            with self.subTest(color=color):
                quality = assess_document_quality(uniform(color), "back")
                self.assertFalse(quality.readable)
                self.assertIn("document_back_too_blurred_or_blank", quality.reasons)

    def test_small_but_sharp_image_is_still_unreadable(self):
        quality = assess_document_quality(document(size=(240, 160)), "front")
        self.assertIn("document_front_resolution_insufficient", quality.reasons)
        self.assertFalse(quality.readable)

    def test_exif_orientation_uses_display_dimensions(self):
        original = document(encoding="JPEG")
        with Image.open(BytesIO(original.content)) as image:
            exif = image.getexif()
            exif[274] = 6
            buffer = BytesIO()
            image.save(buffer, format="JPEG", exif=exif)
        rotated = ImageInput(buffer.getvalue(), original.width, original.height, "JPEG")
        quality = assess_document_quality(rotated, "front")
        self.assertEqual(quality.diagnostics["width"], original.height)
        self.assertEqual(quality.diagnostics["height"], original.width)
        self.assertTrue(quality.readable)


class CaptureScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.front = document(0)
        cls.back = document(1)
        cls.selfie = document(2)
        cls.front_quality = assess_document_quality(cls.front, "front")
        cls.back_quality = assess_document_quality(cls.back, "back")

    def score(self, face, pad, **options):
        return indicative_score(
            face,
            pad,
            self.front_quality,
            self.back_quality,
            demo=False,
            identical=False,
            document_failed=False,
            **options,
        )

    def test_configured_signals_produce_capped_indication_and_never_approval(self):
        result = service().verify(self.front, self.back, self.selfie)
        self.assertEqual(result.status, VerificationStatus.REVIEW)
        self.assertEqual(result.score.kind, "indicative")
        self.assertEqual(result.score.value, 6)
        self.assertEqual(result.score.evidence_ceiling, 6)
        self.assertFalse(result.score.authenticity_confirmed)
        self.assertEqual(result.checks.document_back.outcome, Outcome.INCONCLUSIVE)
        self.assertIn(
            "back_document_authenticity_not_implemented", result.checks.document_back.reasons
        )
        self.assertTrue(result.limitations)

    def test_even_all_provider_passes_cannot_approve_unverified_back(self):
        configured = service(
            face=measurement("face", 0.99, outcome=Outcome.PASS),
            liveness=measurement("liveness", 0.99, outcome=Outcome.PASS),
            document_outcome=Outcome.PASS,
        )
        result = configured.verify(self.front, self.back, self.selfie)
        self.assertEqual(result.status, VerificationStatus.REVIEW)
        self.assertFalse(result.score.authenticity_confirmed)

    def test_demo_uses_no_fabricated_score(self):
        demo = CaptureService(
            VerifyService(DemoFaceEngine(), DemoLivenessEngine(), DemoDocumentEngine(), demo=True)
        )
        result = demo.verify(self.front, self.back, self.selfie)
        self.assertIsNone(result.score.value)
        self.assertEqual(result.score.kind, "unavailable")
        self.assertEqual(result.status, VerificationStatus.REVIEW)
        self.assertIn("demonstração", result.score.explanation)

    def test_missing_scores_and_unknown_measurement_kind_are_unavailable(self):
        face = measurement("face", None)
        pad = measurement("liveness", 0.9)
        self.assertIsNone(self.score(face, pad).value)
        face = measurement("face", 0.9).model_copy(update={"score_kind": "unknown"})
        self.assertIsNone(self.score(face, pad).value)
        self.assertIsNone(self.score(measurement("face", 0.9), measurement("liveness", -0.1)).value)

    def test_disagreeing_liveness_ensemble_is_unavailable_despite_high_score(self):
        for reasons, diagnostics in [
            (["liveness_ensemble_disagreement"], {}),
            (["liveness_threshold_not_locally_calibrated"], {"ensemble_disagreement": 0.8}),
        ]:
            with self.subTest(reasons=reasons):
                pad = measurement("liveness", 0.95, reasons=reasons, diagnostics=diagnostics)
                self.assertIsNone(self.score(measurement("face", 0.95), pad).value)

    def test_unknown_inconclusive_errors_never_receive_a_numeric_score(self):
        face = measurement("face", 0.99, reasons=["face_pose_unsupported"])
        self.assertIsNone(self.score(face, measurement("liveness", 0.99)).value)
        pad = measurement("liveness", 0.99, reasons=["capture_integrity_unavailable"])
        self.assertIsNone(self.score(measurement("face", 0.99), pad).value)
        face = measurement("face", 0.99).model_copy(update={"reasons": []})
        self.assertIsNone(self.score(face, measurement("liveness", 0.99)).value)

    def test_weaker_face_or_pad_evidence_lowers_the_rating(self):
        values = [
            self.score(measurement("face", face), measurement("liveness", pad)).value
            for face, pad in [(0.9, 0.9), (0.4, 0.9), (0.4, 0.1), (-0.2, 0.9)]
        ]
        self.assertGreater(values[0], values[1])
        self.assertGreater(values[1], values[2])
        self.assertGreaterEqual(values[2], values[3])
        self.assertEqual(values[3], 0)

    def test_calibrated_rejection_is_preserved_and_cannot_receive_high_rating(self):
        for role in ["face", "liveness", "document"]:
            with self.subTest(role=role):
                configured = service(
                    face=measurement(
                        "face", 0.95, outcome=Outcome.FAIL if role == "face" else Outcome.PASS
                    ),
                    liveness=measurement(
                        "liveness",
                        0.95,
                        outcome=Outcome.FAIL if role == "liveness" else Outcome.PASS,
                    ),
                    document_outcome=Outcome.FAIL if role == "document" else Outcome.PASS,
                )
                result = configured.verify(self.front, self.back, self.selfie)
                self.assertEqual(result.status, VerificationStatus.REJECTED)
                self.assertLessEqual(result.score.value, 2)

    def test_unreadable_side_prevents_numeric_rating(self):
        for front, back in [(uniform(), self.back), (self.front, uniform())]:
            with self.subTest(front=front is self.front):
                result = service().verify(front, back, self.selfie)
                self.assertIsNone(result.score.value)
                self.assertEqual(result.status, VerificationStatus.REVIEW)

    def test_identical_sides_are_rejected_and_score_zero(self):
        result = service().verify(self.front, self.front, self.selfie)
        self.assertEqual(result.status, VerificationStatus.REJECTED)
        self.assertEqual(result.score.value, 0)
        self.assertIn("document_sides_identical", result.checks.document_back.reasons)

    def test_same_pixels_with_different_metadata_are_duplicate_sides(self):
        with Image.open(BytesIO(self.front.content)) as image:
            from PIL.PngImagePlugin import PngInfo

            metadata = PngInfo()
            metadata.add_text("research", "synthetic unit test")
            buffer = BytesIO()
            image.save(buffer, format="PNG", pnginfo=metadata)
        alternative = ImageInput(buffer.getvalue(), self.front.width, self.front.height, "PNG")
        self.assertNotEqual(alternative.content, self.front.content)
        result = service().verify(self.front, alternative, self.selfie)
        self.assertEqual(result.status, VerificationStatus.REJECTED)
        self.assertIn("document_sides_identical", result.reasons)

    def test_public_result_contains_no_image_or_quality_fingerprint(self):
        result = service().verify(self.front, self.back, self.selfie)
        serialized = result.model_dump_json()
        self.assertNotIn(self.front.content.hex(), serialized)
        self.assertNotIn(self.front_quality.fingerprint.hex(), serialized)
        self.assertNotIn('"fingerprint"', serialized)
        self.assertNotIn('"portrait"', serialized)
        self.assertNotIn('"embedding"', serialized)

    def test_schema_prevents_score_availability_and_authenticity_contradictions(self):
        for payload in [
            {"value": 5, "kind": "unavailable"},
            {"value": None, "kind": "indicative"},
            {"value": 7, "kind": "indicative"},
            {"value": 11, "kind": "indicative"},
            {"value": 5, "kind": "indicative", "authenticity_confirmed": True},
            {"value": float("nan"), "kind": "indicative"},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                CaptureScore(**payload, explanation="Synthetic test")


if __name__ == "__main__":
    unittest.main()
