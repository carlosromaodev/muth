import unittest
from io import BytesIO
from unittest.mock import Mock, patch

from PIL import Image

from muth.document_image import PreparedDocument
from muth.media import ImageInput
from tests.test_capture_scoring import document, service


class CaptureGeometryTests(unittest.TestCase):
    def test_portrait_and_quality_use_isolated_card_ocr_receives_original_scene(self):
        front, back, selfie = document(0), document(1), document(2)
        card = document(size=(240, 160))
        with Image.open(BytesIO(card.content)) as source:
            prepared = PreparedDocument(
                image=source.copy(),
                source_isolated=True,
                strategy="quadrilateral",
                reasons=("document_region_isolated",),
                native_size=source.size,
            )
        pipeline = service()
        ocr = Mock(wraps=pipeline.document_engine)
        pipeline.document_engine = ocr
        analyze = Mock(wraps=pipeline.verify_service.document.analyze)
        pipeline.verify_service.document.analyze = analyze
        with patch("muth.services.capture.prepare_document", return_value=prepared):
            result = pipeline.verify(front, back, selfie)
        reference = analyze.call_args.args[0]
        self.assertIsInstance(reference, ImageInput)
        self.assertEqual((reference.width, reference.height), (240, 160))
        self.assertIsNone(result.score.value)
        self.assertIn("document_front_resolution_insufficient", result.reasons)
        ocr.extract.assert_called_once_with(front, back)

    def test_uncertain_geometry_preserves_original_reference(self):
        front, back, selfie = document(0), document(1), document(2)
        fallback = PreparedDocument(
            image=Image.new("RGB", (800, 600)),
            source_isolated=False,
            strategy="original",
            reasons=("document_geometry_uncertain",),
            native_size=(800, 600),
        )
        pipeline = service()
        analyze = Mock(wraps=pipeline.verify_service.document.analyze)
        pipeline.verify_service.document.analyze = analyze
        with patch("muth.services.capture.prepare_document", return_value=fallback):
            pipeline.verify(front, back, selfie)
        self.assertIs(analyze.call_args.args[0], front)

    def test_transparent_verso_cannot_support_a_score_with_hidden_text(self):
        front, back, selfie = document(0), document(1, size=(900, 600)), document(2)
        with Image.open(BytesIO(back.content)) as source:
            hidden = source.convert("RGBA")
            hidden.putalpha(0)
            content = BytesIO()
            hidden.save(content, format="PNG")
        transparent_back = ImageInput(content.getvalue(), back.width, back.height, "PNG")
        result = service().verify(front, transparent_back, selfie)
        self.assertIsNone(result.score.value)
        self.assertIn("document_back_transparency_unsupported", result.reasons)
        self.assertIn("document_transparency_unsupported", result.reasons)
