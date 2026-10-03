"""Transient front/back/selfie orchestration and explicitly indicative browser rating."""

import hashlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageFilter, ImageOps, ImageStat

from muth.capture_models import CaptureChecks, CaptureScore, CaptureVerification
from muth.document_image import prepare_document
from muth.domain import Check, DocumentCheck, Outcome, VerificationStatus
from muth.media import ImageInput
from muth.ocr import TesseractDocumentEngine
from muth.services.verify import VerifyService

SCORING_VERSION = "capture-indicative-v1"
DOCUMENT_QUALITY_VERSION = "pillow-edge-exposure-v1"
EVIDENCE_CEILING = 6.0
LIMITATIONS = [
    "A nota é um indicador técnico; não é uma probabilidade nem uma certificação de identidade.",
    "A autenticidade do documento e do verso ainda não é verificada; não há consulta oficial.",
    "Os limiares de qualidade e a escala de apresentação exigem validação com documentos locais.",
    "Liveness RGB não comprova captura recente nem exclui injecção de vídeo ou deepfakes.",
]


@dataclass(frozen=True)
class DocumentQuality:
    readable: bool
    value: float
    reasons: list[str]
    diagnostics: dict[str, float]
    fingerprint: bytes


def assess_document_quality(image: ImageInput, side: str) -> DocumentQuality:
    """Image readability only. Neither edges nor dimensions establish document authenticity."""
    with Image.open(BytesIO(image.content)) as source:
        oriented = ImageOps.exif_transpose(source)
        width, height = oriented.size
        transparent = False
        if "A" in oriented.getbands() or "transparency" in oriented.info:
            rgba = oriented.convert("RGBA")
            transparent = rgba.getchannel("A").getextrema()[0] < 255
            if transparent:
                # Quality must describe visible pixels, including the document verso.
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                oriented = Image.alpha_composite(background, rgba).convert("RGB")
        # The media layer has already fully decoded and bounded the input.
        oriented.thumbnail((1024, 1024))
        frame = oriented.convert("RGB")
        digest = hashlib.sha256()
        digest.update(f"{width}:{height}:".encode("ascii"))
        digest.update(frame.tobytes())
        grayscale = frame.convert("L")
        histogram = grayscale.histogram()
        pixels = grayscale.width * grayscale.height
        dark = sum(histogram[:6]) / pixels
        bright = sum(histogram[250:]) / pixels
        # FIND_EDGES leaves a one-pixel border. Exclude it to avoid false sharpness on blank images.
        edges = grayscale.filter(ImageFilter.FIND_EDGES)
        if edges.width > 2 and edges.height > 2:
            edges = edges.crop((1, 1, edges.width - 1, edges.height - 1))
        edge_variance = float(ImageStat.Stat(edges).var[0])

    short, long = sorted((width, height))
    reasons = []
    if transparent:
        reasons.append(f"document_{side}_transparency_unsupported")
    if short < 320 or long < 480:
        reasons.append(f"document_{side}_resolution_insufficient")
    if max(dark, bright) > 0.95:
        reasons.append(f"document_{side}_exposure_unsupported")
    if edge_variance < 20:
        reasons.append(f"document_{side}_too_blurred_or_blank")
    resolution = min(1.0, short / 720, long / 1080)
    sharpness = min(1.0, edge_variance / 100)
    exposure = max(0.0, 1 - dark - bright)
    quality = 0.4 * resolution + 0.4 * sharpness + 0.2 * exposure
    return DocumentQuality(
        readable=not reasons,
        value=quality,
        reasons=reasons,
        diagnostics={
            "width": float(width),
            "height": float(height),
            "edge_variance": edge_variance,
            "dark_clipped_fraction": dark,
            "bright_clipped_fraction": bright,
            "readability_index": quality,
        },
        fingerprint=digest.digest(),
    )


def front_check(check: DocumentCheck, quality: DocumentQuality) -> DocumentCheck:
    diagnostics = {**check.diagnostics, **quality.diagnostics}
    # Retain a provider's rejection; quality is never sufficient to approve a document.
    outcome = check.outcome
    if not quality.readable and outcome != Outcome.FAIL:
        outcome = Outcome.INCONCLUSIVE
    return check.model_copy(
        update={
            "outcome": outcome,
            "reasons": [*check.reasons, *quality.reasons],
            "diagnostics": diagnostics,
        }
    )


def back_check(quality: DocumentQuality, *, identical: bool) -> Check:
    return Check(
        outcome=Outcome.FAIL if identical else Outcome.INCONCLUSIVE,
        reasons=[
            *(["document_sides_identical"] if identical else []),
            *quality.reasons,
            "back_document_authenticity_not_implemented",
        ],
        provider="muth-document-quality",
        model_version=DOCUMENT_QUALITY_VERSION,
        diagnostics=quality.diagnostics,
    )


def indicative_score(
    face: Check,
    liveness: Check,
    front: DocumentQuality,
    back: DocumentQuality,
    *,
    demo: bool,
    identical: bool,
    document_failed: bool,
) -> CaptureScore:
    unavailable = None
    if demo:
        unavailable = "Modo de demonstração: não existe uma nota biométrica real disponível."
    elif not front.readable or not back.readable:
        unavailable = "Recapture os documentos com mais definição, resolução e iluminação."
    elif face.score is None or liveness.score is None:
        unavailable = "Os motores não obtiveram sinais suficientes para calcular uma nota."
    elif face.score_kind != "cosine_similarity" or liveness.score_kind != "liveness_softmax":
        unavailable = "O tipo de medição dos motores não suporta esta escala de apresentação."
    elif not 0 <= liveness.score <= 1:
        unavailable = "O motor de liveness devolveu uma medição incompatível com esta escala."
    elif (
        "liveness_ensemble_disagreement" in liveness.reasons
        or liveness.diagnostics.get("ensemble_disagreement", 0) > 0.5
    ):
        unavailable = "Os modelos de liveness discordaram; é necessária uma nova captura."
    elif face.outcome == Outcome.INCONCLUSIVE and (
        not face.reasons or set(face.reasons) - {"face_threshold_not_locally_calibrated"}
    ):
        unavailable = "A comparação facial exige uma nova captura ou revisão técnica."
    elif liveness.outcome == Outcome.INCONCLUSIVE and (
        not liveness.reasons
        or set(liveness.reasons)
        - {
            "liveness_threshold_not_locally_calibrated",
            "rgb_pad_does_not_prove_capture_freshness",
        }
    ):
        unavailable = "A captura de liveness exige uma nova captura ou revisão técnica."
    if unavailable is not None:
        return CaptureScore(kind="unavailable", explanation=unavailable)

    assert face.score is not None and liveness.score is not None
    # Presentation heuristic, not calibration: weakest evidence reduces the product.
    face_signal = max(0.0, face.score)
    quality = min(front.value, back.value)
    raw = 10 * face_signal * liveness.score * quality
    value = min(EVIDENCE_CEILING, raw)
    if identical:
        value = 0.0
    elif document_failed or Outcome.FAIL in {face.outcome, liveness.outcome}:
        value = min(value, 2.0)
    return CaptureScore(
        value=round(value, 1),
        kind="indicative",
        explanation=(
            "Indicador = 10 × máximo(0, similaridade facial) × sinal de liveness × "
            "menor índice de legibilidade dos documentos. Limitado a 6/10 enquanto a "
            "autenticidade documental e a escala não estiverem validadas. Sinais reprovados "
            "limitam a nota a 2/10; frente e verso iguais dão 0. Não confirma autenticidade."
        ),
    )


class CaptureService:
    """Uses existing configured engines; does not retain images, embeddings or quality digests."""

    def __init__(self, verify_service: VerifyService, document_engine=None):
        self.verify_service = verify_service
        self.document_engine = document_engine or TesseractDocumentEngine(enabled=False)

    def verify(
        self, document_front: ImageInput, document_back: ImageInput, selfie: ImageInput
    ) -> CaptureVerification:
        front_region = prepare_document(document_front)
        back_region = prepare_document(document_back)
        front_image = (
            front_region.as_image_input() if front_region.source_isolated else document_front
        )
        back_image = back_region.as_image_input() if back_region.source_isolated else document_back
        front = assess_document_quality(front_image, "front")
        back = assess_document_quality(back_image, "back")
        identical = (
            document_front.content == document_back.content or front.fingerprint == back.fingerprint
        )
        verification = self.verify_service.verify(front_image, selfie)
        front_document = front_check(verification.checks.document, front)
        back_document = back_check(back, identical=identical)
        status = VerificationStatus.REVIEW
        if verification.status == VerificationStatus.REJECTED or identical:
            status = VerificationStatus.REJECTED
        score = indicative_score(
            verification.checks.face_match,
            verification.checks.liveness,
            front,
            back,
            demo=self.verify_service.demo,
            identical=identical,
            document_failed=front_document.outcome == Outcome.FAIL,
        )
        return CaptureVerification(
            verification_id=verification.verification_id,
            created_at=verification.created_at,
            status=status,
            mode=verification.mode,
            score=score,
            checks=CaptureChecks(
                face_match=verification.checks.face_match,
                liveness=verification.checks.liveness,
                document_front=front_document,
                document_back=back_document,
            ),
            limitations=list(LIMITATIONS),
            scoring_version=SCORING_VERSION,
            reasons=[
                *verification.reasons,
                *front.reasons,
                *back_document.reasons,
                *front_region.reasons,
                *back_region.reasons,
                "capture_rating_is_indicative",
            ],
            document_data=self.document_engine.extract(document_front, document_back),
        )
