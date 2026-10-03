"""Same-origin browser flow using session-scoped bearer capabilities."""

import asyncio
import logging
import time
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from muth.api import current_service, invoke_engine
from muth.capture_learning import CaptureDocumentCorrections
from muth.capture_models import CaptureVerification
from muth.capture_security import capture_fingerprint
from muth.document_image import geometry_available
from muth.domain import Consent, CreateSession
from muth.errors import MuthError
from muth.live_camera import CameraAssessment, CameraTarget, assess_camera
from muth.media import read_image
from muth.security import Principal
from muth.services.capture import CaptureService

logger = logging.getLogger("muth.capture")
router = APIRouter(prefix="/capture-api", tags=["Captura web"])


class BrowserConsent(BaseModel):
    model_config = {"extra": "forbid"}
    accepted: bool = Field(strict=True)
    camera_frames_opt_in: bool = Field(default=False, strict=True)
    camera_policy_version: str | None = Field(default=None, min_length=1, max_length=80)
    purpose: Literal["onboarding"]
    policy_version: str = Field(min_length=1, max_length=80)
    learning_opt_in: bool = Field(default=False, strict=True)
    learning_policy_version: str | None = Field(default=None, min_length=1, max_length=80)
    identity_enrollment_opt_in: bool = Field(default=False, strict=True)
    identity_enrollment_policy_version: str | None = Field(
        default=None, min_length=1, max_length=80
    )


class BrowserSessionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    consent: BrowserConsent
    device_group: Literal["android_entry", "android_modern", "ios", "web", "unknown"] = "unknown"


@router.get("/config")
def config(request: Request):
    settings = request.app.state.settings
    ocr = request.app.state.document_ocr
    inference_ready = request.app.state.runtime is not None
    ocr_ready = bool(getattr(ocr, "executable", None))
    return {
        "enabled": settings.capture_portal_enabled and settings.engine_mode != "disabled",
        "mode": settings.engine_mode,
        "biometric_inference_ready": inference_ready,
        "document_ocr_ready": ocr_ready,
        "document_ocr_languages": getattr(ocr, "languages", "") or None,
        "document_preprocessing_ready": geometry_available(),
        "live_camera_enabled": settings.live_camera_enabled,
        "live_camera_policy_version": settings.live_camera_policy_version,
        "live_camera_interval_ms": settings.live_camera_interval_ms,
        "live_document_detector_ready": settings.live_camera_enabled and geometry_available(),
        "live_face_detector_ready": settings.live_camera_enabled and inference_ready,
        "live_camera_max_frame_bytes": settings.live_camera_max_frame_bytes,
        "live_camera_max_frame_dimension": settings.live_camera_max_frame_dimension,
        "setup_required": settings.engine_mode == "demo" or not ocr_ready,
        "privacy_policy_version": settings.capture_policy_version,
        "max_upload_bytes": settings.max_upload_bytes,
        "max_request_bytes": settings.capture_max_request_bytes,
        "max_image_pixels": settings.max_image_pixels,
        "session_ttl_seconds": settings.session_ttl_seconds,
        "retention_days": settings.capture_retention_days,
        "document_authenticity_supported": False,
        "document_ocr_enabled": settings.document_ocr_enabled,
        "learning_collection_enabled": settings.capture_learning_enabled
        and settings.learning_enabled,
        "learning_policy_version": settings.capture_learning_policy_version,
        "learning_retention_days": settings.capture_learning_retention_days,
        "identity_enrollment_enabled": settings.identity_enrollment_enabled
        and request.app.state.runtime is not None,
        "identity_enrollment_policy_version": settings.identity_enrollment_policy_version,
        "identity_retention_days": settings.identity_retention_days,
    }


def _camera_session_active(request: Request, session_id: str):
    principal = request.app.state.capture_access.authenticate(
        request.headers.get("authorization"), session_id
    )
    session = request.app.state.store.get(principal, session_id)
    if session.expires_at.timestamp() <= request.app.state.store.clock():
        raise MuthError(410, "session_expired", "O prazo da captura terminou. Recomece.")
    if session.status != "awaiting_capture":
        raise MuthError(409, "camera_session_inactive", "A sessão já não aceita capturas ao vivo.")
    if (
        not session.consent.camera_frames_opt_in
        or session.consent.camera_policy_version
        != request.app.state.settings.live_camera_policy_version
    ):
        raise MuthError(403, "camera_consent_required", "Autorize a análise ao vivo desta sessão.")


@router.post("/sessions/{session_id}/camera-assessment", response_model=CameraAssessment)
async def camera_assessment(request: Request, session_id: str, target: CameraTarget):
    settings = request.app.state.settings
    preview_settings = settings.model_copy(
        update={
            "max_upload_bytes": settings.live_camera_max_frame_bytes,
            "max_image_pixels": 2_000_000,
        }
    )
    deadline = getattr(request.state, "camera_deadline", time.monotonic() + 10)
    try:
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            async with request.form(
                max_files=1, max_fields=0, max_part_size=settings.live_camera_max_frame_bytes
            ) as form:
                if list(form.keys()) != ["frame"] or len(form.multi_items()) != 1:
                    raise MuthError(
                        422, "camera_parts_invalid", "Envie apenas uma imagem de pré-visualização."
                    )
                if not isinstance(form["frame"], UploadFile):
                    raise MuthError(422, "camera_parts_invalid", "A captura deve ser uma imagem.")
                image = await read_image(
                    form["frame"], preview_settings, capacity=request.app.state.capacity
                )
            if max(image.width, image.height) > settings.live_camera_max_frame_dimension:
                raise MuthError(
                    413, "camera_frame_too_large", "Reduza a resolução da pré-visualização."
                )
            await run_in_threadpool(_camera_session_active, request, session_id)

            def analyze():
                with request.app.state.capacity.worker():
                    try:
                        return assess_camera(image, target, runtime=request.app.state.runtime)
                    except MuthError:
                        raise
                    except Exception as exc:
                        raise MuthError(
                            503, "camera_detector_failure", "A detecção ao vivo está indisponível."
                        ) from exc

            result = await run_in_threadpool(analyze)
            await run_in_threadpool(_camera_session_active, request, session_id)
            return result
    except TimeoutError as exc:
        raise MuthError(
            408, "camera_assessment_timeout", "A detecção demorou demasiado. Repita."
        ) from exc


@router.post("/sessions", status_code=201)
async def create(request: Request, payload: BrowserSessionRequest):
    settings = request.app.state.settings
    if (
        not payload.consent.accepted
        or payload.consent.policy_version != settings.capture_policy_version
    ):
        raise MuthError(
            422, "capture_consent_required", "Confirme a política de privacidade actual."
        )
    principal = Principal(settings.capture_tenant_id, "browser-capture", frozenset({"verify"}))
    if payload.consent.camera_frames_opt_in and (
        not settings.live_camera_enabled
        or payload.consent.camera_policy_version != settings.live_camera_policy_version
    ):
        raise MuthError(422, "camera_consent_required", "Confirme a política de captura ao vivo.")
    if payload.consent.identity_enrollment_opt_in and (
        not settings.identity_enrollment_enabled
        or request.app.state.runtime is None
        or payload.consent.identity_enrollment_policy_version
        != settings.identity_enrollment_policy_version
    ):
        raise MuthError(
            422, "identity_consent_required", "Confirme a política para guardar a identidade."
        )
    if payload.consent.learning_opt_in and (
        not settings.capture_learning_enabled
        or not settings.learning_enabled
        or payload.consent.learning_policy_version != settings.capture_learning_policy_version
    ):
        raise MuthError(
            422, "learning_consent_required", "Confirme a política de contribuição actual."
        )
    session = await run_in_threadpool(
        request.app.state.capture_store.create,
        principal,
        CreateSession(
            consent=Consent(
                accepted=True,
                purpose=payload.consent.purpose,
                policy_version=payload.consent.policy_version,
                camera_frames_opt_in=payload.consent.camera_frames_opt_in,
                camera_policy_version=payload.consent.camera_policy_version
                if payload.consent.camera_frames_opt_in
                else None,
            ),
            device_group=payload.device_group,
        ),
        request.state.request_id,
        learning_opt_in=payload.consent.learning_opt_in,
        identity_enrollment_opt_in=payload.consent.identity_enrollment_opt_in,
    )
    return {
        "session_id": session.session_id,
        "capture_token": request.app.state.capture_access.issue(session),
        "expires_at": session.expires_at,
        "camera_frames_enabled": session.consent.camera_frames_opt_in,
    }


@router.get("/sessions/{session_id}", response_model=CaptureVerification)
async def result(request: Request, session_id: str):
    report = await run_in_threadpool(
        request.app.state.capture_store.result, request.state.capture_principal, session_id
    )
    return await identity_access(request, report)


async def identity_access(request, report):
    if report.identity.identity_id:
        try:
            identity = await run_in_threadpool(
                request.app.state.identities.get,
                request.state.capture_principal,
                report.identity.identity_id,
            )
            report.identity.identity_token = request.app.state.identity_access.issue(identity)
        except MuthError as exc:
            if exc.status != 404:
                raise
            report.identity.status = "deleted"
            report.identity.biometric_template_saved = False
            report.identity.explanation = "O registo guardado já foi eliminado ou expirou."
    return report


@router.post("/sessions/{session_id}/document-corrections")
async def document_corrections(
    request: Request, session_id: str, payload: CaptureDocumentCorrections
):
    info = await run_in_threadpool(
        request.app.state.capture_learning.propose,
        request.state.capture_principal,
        session_id,
        payload,
        request.state.request_id,
    )
    return {"learning": info, "proposed_fields": payload.fields}


@router.delete("/sessions/{session_id}/learning-consent")
async def withdraw_learning(request: Request, session_id: str):
    info = await run_in_threadpool(
        request.app.state.capture_learning.withdraw,
        request.state.capture_principal,
        session_id,
        request.state.request_id,
    )
    return {"learning": info, "proposed_fields": {}}


@router.delete("/sessions/{session_id}", status_code=204)
async def delete(request: Request, session_id: str):
    await run_in_threadpool(
        request.app.state.store.delete,
        request.state.capture_principal,
        session_id,
        request.state.request_id,
    )
    return Response(status_code=204)


@router.post("/sessions/{session_id}/verify", response_model=CaptureVerification)
async def verify(
    request: Request,
    session_id: str,
    idempotency_key: Annotated[
        str, Header(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")
    ],
):
    settings, store = request.app.state.settings, request.app.state.store
    principal = request.state.capture_principal
    session = await run_in_threadpool(store.get, principal, session_id)
    names = ("document_front", "document_back", "selfie")
    async with request.form(
        max_files=3, max_fields=0, max_part_size=settings.max_upload_bytes
    ) as form:
        if sorted(form.keys()) != sorted(names) or len(form.multi_items()) != 3:
            raise MuthError(
                422, "capture_parts_invalid", "Envie frente, verso e selfie uma vez cada."
            )
        if any(not isinstance(form[name], UploadFile) for name in names):
            raise MuthError(422, "capture_parts_invalid", "As três capturas devem ser imagens.")
        images = []
        for name in names:
            images.append(
                await read_image(form[name], settings, capacity=request.app.state.capacity)
            )
    fingerprint = await run_in_threadpool(
        capture_fingerprint, store, principal, session_id, *images
    )
    claim_task = asyncio.create_task(
        run_in_threadpool(
            store.claim,
            principal,
            session_id,
            idempotency_key,
            fingerprint,
            request.state.request_id,
        )
    )
    claim = None
    try:
        claim = await asyncio.shield(claim_task)
        if claim.replay is not None:
            report = await run_in_threadpool(
                request.app.state.capture_store.result, principal, session_id
            )
            return await identity_access(request, report)
        service = await run_in_threadpool(current_service, request, principal, session.device_group)
        report = await run_in_threadpool(
            invoke_engine,
            request,
            CaptureService(service, request.app.state.document_ocr).verify,
            *images,
        )
        enrollment_material = None
        if await run_in_threadpool(
            request.app.state.capture_store.enrollment_requested, principal, session_id
        ):
            from muth.api import current_engines

            bundle = await run_in_threadpool(
                current_engines, request, principal, session.device_group
            )
            report.identity, enrollment_material = await run_in_threadpool(
                invoke_engine,
                request,
                request.app.state.identity_service.prepare,
                bundle,
                images[2],
                report,
            )
        await run_in_threadpool(
            request.app.state.capture_store.complete,
            principal,
            session_id,
            claim.attempt,
            report,
            request.state.request_id,
            enrollment_material,
        )
        return await identity_access(request, report)
    except asyncio.CancelledError:

        async def cleanup():
            try:
                pending = await claim_task
                if pending.replay is None:
                    await run_in_threadpool(
                        store.release,
                        principal,
                        session_id,
                        pending.attempt,
                        request.state.request_id,
                    )
            except Exception as exc:
                logger.warning("capture_cleanup_failed exception_type=%s", type(exc).__name__)

        await asyncio.shield(cleanup())
        raise
    except Exception:
        if claim is not None:
            await run_in_threadpool(
                store.release, principal, session_id, claim.attempt, request.state.request_id
            )
        raise
