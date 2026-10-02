from typing import Protocol

from muth.domain import Check
from muth.media import ImageInput


class LivenessEngine(Protocol):
    def assess(self, selfie: ImageInput) -> Check:
        """Avaliar ataques de apresentação; proteção contra injeção é uma camada adicional."""
        ...
