"""Read local prerequisites without exposing credentials or document data."""

from importlib.util import find_spec
from pathlib import Path

from muth.document_image import geometry_available
from muth.engines.biometric import load_manifest
from muth.ocr import TesseractDocumentEngine


def inspect_setup(settings):
    dependencies = all(find_spec(name) is not None for name in ("cv2", "numpy", "onnxruntime"))
    geometry = geometry_available()
    assets = False
    if settings.biometric_manifest:
        try:
            load_manifest(Path(settings.biometric_manifest))
            assets = True
        except (OSError, ValueError):
            pass
    ocr = TesseractDocumentEngine(
        enabled=settings.document_ocr_enabled,
        executable=settings.document_ocr_executable,
        languages=settings.document_ocr_languages,
        timeout_seconds=settings.document_ocr_timeout_seconds,
    )
    missing = sorted(
        set(settings.document_ocr_languages.split("+")) - set(ocr.languages.split("+"))
    )
    actions = []
    if not dependencies:
        actions.append("Instala as dependências: uv sync --locked --extra biometrics.")
    if not assets:
        actions.append(
            "Prepara os pesos com scripts/fetch_biometrics.py e scripts/export_liveness.py; "
            "valida e activa com muth activate-biometrics."
        )
    if settings.engine_mode != "biometric":
        actions.append("Executa muth activate-biometrics e reinicia o servidor.")
    if not ocr.executable or missing:
        actions.append(
            "Instala Tesseract e os idiomas OCR configurados. "
            "A imagem Docker do MUTH já inclui português e inglês."
        )
    if not geometry:
        actions.append("Instala as dependências de recorte documental: uv sync --locked.")
    return {
        "status": "setup_required" if actions else "ready_for_research",
        "mode": settings.engine_mode,
        "biometric_assets_verified": assets,
        "biometric_dependencies_ready": dependencies,
        "document_preprocessing_ready": geometry,
        "document_ocr_ready": bool(ocr.executable),
        "document_ocr_languages": ocr.languages or None,
        "missing_ocr_languages": missing,
        "identity_verification_ready": False,
        "actions": actions,
    }
