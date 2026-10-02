from dataclasses import dataclass

from muth.engines.demo import DemoDocumentEngine, DemoFaceEngine, DemoLivenessEngine
from muth.engines.face import FaceEngine
from muth.engines.id import DocumentEngine
from muth.engines.liveness import LivenessEngine


@dataclass(frozen=True)
class EngineBundle:
    face: FaceEngine
    liveness: LivenessEngine
    document: DocumentEngine

    @classmethod
    def demo(cls) -> "EngineBundle":
        return cls(DemoFaceEngine(), DemoLivenessEngine(), DemoDocumentEngine())
