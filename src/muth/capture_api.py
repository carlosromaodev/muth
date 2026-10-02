"""Same-origin browser flow using session-scoped bearer capabilities."""

import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from muth.api import current_service, invoke_engine
from muth.capture_models import CaptureVerification
from muth.capture_security import capture_fingerprint
from muth.domain import Consent, CreateSession
from muth.errors import MuthError
from muth.media import read_image
from muth.security import Principal
from muth.services.capture import CaptureService

logger = logging.getLogger("muth.capture")
router = APIRouter(prefix="/capture-api", tags=["Captura web"])


class BrowserConsent(BaseModel):
    model_config = {"extra": "forbid"}
    accepted: bool = Field(strict=True)
    purpose: Literal["onboarding"]
    policy_version: str = Field(min_length=1, max_length=80)


class BrowserSessionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    consent: BrowserConsent
    device_group: Literal["android_entry", "android_modern", "ios", "web", "unknown"] = "unknown"


@router.get("/config")
def config(request: Request):
    settings = request.app.state.settings
    return {
        "enabled": settings.capture_portal_enabled and settings.engine_mode != "disabled",
        "mode": settings.engine_mode,
        "privacy_policy_version": settings.capture_policy_version,
        "max_upload_bytes": settings.max_upload_bytes,
        "max_request_bytes": settings.capture_max_request_bytes,
        "max_image_pixels": settings.max_image_pixels,
        "session_ttl_seconds": settings.session_ttl_seconds,
        "retention_days": settings.capture_retention_days,
        "document_authenticity_supported": False,
    }


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
    session = await run_in_threadpool(
        request.app.state.capture_store.create,
        principal,
        CreateSession(
            consent=Consent(**payload.consent.model_dump()), device_group=payload.device_group
        ),
        request.state.request_id,
    )
    return {
        "session_id": session.session_id,
        "capture_token": request.app.state.capture_access.issue(session),
        "expires_at": session.expires_at,
    }


@router.get("/sessions/{session_id}", response_model=CaptureVerification)
async def result(request: Request, session_id: str):
    return await run_in_threadpool(
        request.app.state.capture_store.result, request.state.capture_principal, session_id
    )


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
            return await run_in_threadpool(
                request.app.state.capture_store.result, principal, session_id
            )
        service = await run_in_threadpool(current_service, request, principal, session.device_group)
        report = await run_in_threadpool(
            invoke_engine, request, CaptureService(service).verify, *images
        )
        await run_in_threadpool(
            request.app.state.capture_store.complete,
            principal,
            session_id,
            claim.attempt,
            report,
            request.state.request_id,
        )
        return report
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
