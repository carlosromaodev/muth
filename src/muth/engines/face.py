from typing import Protocol

from muth.domain import Check
from muth.media import ImageInput


class FaceEngine(Protocol):
    def compare(self, reference: ImageInput, selfie: ImageInput) -> Check:
        """Comparar dois retratos 1:1 com limiares calibrados por modelo."""
        ...
