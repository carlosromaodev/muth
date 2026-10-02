from datetime import UTC, datetime
from uuid import uuid4

from muth.domain import Check, Checks, Outcome, Verification
from muth.engines.face import FaceEngine
from muth.engines.id import DocumentEngine
from muth.engines.liveness import LivenessEngine
from muth.media import ImageInput
from muth.services.decision import POLICY_VERSION, decide


class VerifyService:
    def __init__(
        self, face: FaceEngine, liveness: LivenessEngine, document: DocumentEngine, *, demo: bool
    ):
        self.face = face
        self.liveness = liveness
        self.document = document
        self.demo = demo

    def verify(self, document: ImageInput, selfie: ImageInput) -> Verification:
        analysis = self.document.analyze(document)
        face_match = (
            self.face.compare(analysis.portrait, selfie)
            if analysis.portrait is not None
            else Check(
                outcome=Outcome.INCONCLUSIVE,
                reasons=["document_portrait_unavailable"],
                provider="muth",
                model_version="orchestrator-v1",
            )
        )
        checks = Checks(
            document=analysis.check,
            liveness=self.liveness.assess(selfie),
            face_match=face_match,
        )
        status, reasons = decide(checks, demo=self.demo)
        return Verification(
            verification_id=f"mth_vfy_{uuid4().hex}",
            created_at=datetime.now(UTC),
            status=status,
            mode="demo" if self.demo else "configured",
            policy_version=POLICY_VERSION,
            reasons=reasons,
            checks=checks,
        )
