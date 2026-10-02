from dataclasses import dataclass
from typing import Protocol

from muth.domain import DocumentCheck
from muth.media import ImageInput


@dataclass(frozen=True)
class DocumentAnalysis:
    check: DocumentCheck
    portrait: ImageInput | None = None


class DocumentEngine(Protocol):
    def analyze(self, document: ImageInput) -> DocumentAnalysis:
        """Classificar, extrair campos e recortar o retrato, sem equiparar OCR a autenticidade."""
        ...
