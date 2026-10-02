from muth.domain import Check, DocumentCheck, Outcome
from muth.engines.id import DocumentAnalysis
from muth.media import ImageInput


class DemoFaceEngine:
    def compare(self, reference: ImageInput, selfie: ImageInput) -> Check:
        return Check(
            outcome=Outcome.INCONCLUSIVE,
            reasons=["demo_face_model_not_configured"],
            provider="demo",
            model_version="demo-v1",
        )


class DemoLivenessEngine:
    def assess(self, selfie: ImageInput) -> Check:
        return Check(
            outcome=Outcome.INCONCLUSIVE,
            reasons=["demo_liveness_model_not_configured"],
            provider="demo",
            model_version="demo-v1",
        )


class DemoDocumentEngine:
    def analyze(self, document: ImageInput) -> DocumentAnalysis:
        return DocumentAnalysis(
            check=DocumentCheck(
                outcome=Outcome.INCONCLUSIVE,
                reasons=["demo_document_model_not_configured"],
                provider="demo",
                model_version="demo-v1",
            )
        )
