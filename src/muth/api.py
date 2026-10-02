import asyncio
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Header, Query, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from muth.domain import (
    AuditEvent,
    Check,
    CreateSession,
    DocumentCheck,
    LearningFeedback,
    ReviewDecision,
    SessionView,
    Verification,
)
from muth.errors import MuthError
from muth.media import read_image
from muth.security import Principal, authenticate
from muth.services.verify import VerifyService

logger = logging.getLogger("muth.engines")

PrincipalDep = Annotated[Principal, Depends(authenticate)]
ImageFile = Annotated[UploadFile, File()]
IdempotencyKey = Annotated[str, Header(min_length=8, max_length=128, pattern=r"^[a-zA-Z0-9_.:-]+$")]


def require_engines(request: Request) -> None:
    if request.app.state.settings.engine_mode == "disabled":
        raise MuthError(503, "engines_unavailable", "Motores reais ainda não configurados.")
    if request.app.state.runtime is not None:
        require_database(request)


def current_engines(request, principal, device_group="unknown"):
    return (
        request.app.state.learning.bundle(principal.tenant_id, device_group)
        or request.app.state.engines
    )


def current_service(request, principal, device_group="unknown"):
    bundle = current_engines(request, principal, device_group)
    return VerifyService(
        bundle.face,
        bundle.liveness,
        bundle.document,
        demo=request.app.state.settings.engine_mode != "biometric",
    )


def invoke_engine(request, operation, *args):
    # The worker retains capacity even if its HTTP task is cancelled before completion.
    with request.app.state.capacity.worker():
        try:
            return operation(*args)
        except MuthError:
            raise
        except Exception as exc:
            logger.warning(
                "engine_failure request_id=%s exception_type=%s",
                request.state.request_id,
                type(exc).__name__,
            )
            raise MuthError(
                503, "engine_failure", "Não foi possível concluir a inferência."
            ) from exc


def require_database(request: Request) -> None:
    if not request.app.state.database.ready():
        raise MuthError(503, "database_not_ready", "Execute a migração da base de dados.")


router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])
engines = APIRouter(dependencies=[Depends(require_engines)])
sessions = APIRouter(prefix="/sessions", dependencies=[Depends(require_database)], tags=["Sessões"])


@sessions.post("", response_model=SessionView, status_code=201)
async def create_session(request: Request, principal: PrincipalDep, payload: CreateSession):
    return await run_in_threadpool(
        request.app.state.store.create, principal, payload, request.state.request_id
    )


@sessions.get("/{session_id}", response_model=SessionView)
async def get_session(request: Request, principal: PrincipalDep, session_id: str):
    return await run_in_threadpool(request.app.state.store.get, principal, session_id)


@sessions.post(
    "/{session_id}/verify", response_model=Verification, dependencies=[Depends(require_engines)]
)
async def verify_session(
    request: Request,
    principal: PrincipalDep,
    session_id: str,
    document: ImageFile,
    selfie: ImageFile,
    idempotency_key: IdempotencyKey,
):
    store = request.app.state.store
    session_view = await run_in_threadpool(store.get, principal, session_id)
    settings = request.app.state.settings
    document_image = await read_image(document, settings, capacity=request.app.state.capacity)
    selfie_image = await read_image(selfie, settings, capacity=request.app.state.capacity)
    fingerprint = await run_in_threadpool(
        store.fingerprint, principal, session_id, document_image, selfie_image
    )
    # Preserve the claim result even if HTTP cancellation arrives while SQLite
    # is still granting it. Otherwise its attempt token would be lost to cleanup.
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
            return claim.replay
        service = await run_in_threadpool(
            current_service, request, principal, session_view.device_group
        )
        result = await run_in_threadpool(
            invoke_engine, request, service.verify, document_image, selfie_image
        )
        await run_in_threadpool(
            store.complete, principal, session_id, claim.attempt, result, request.state.request_id
        )
        return result
    except asyncio.CancelledError:

        async def release_cancelled_claim():
            try:
                completed_claim = await claim_task
                if completed_claim.replay is None:
                    await run_in_threadpool(
                        store.release,
                        principal,
                        session_id,
                        completed_claim.attempt,
                        request.state.request_id,
                    )
            except Exception as exc:
                logger.warning(
                    "claim_cleanup_failed request_id=%s exception_type=%s",
                    request.state.request_id,
                    type(exc).__name__,
                )

        # Native inference may continue; its worker still occupies capacity.
        # Releasing the token prevents its result from finalizing a later attempt.
        await asyncio.shield(release_cancelled_claim())
        raise
    except Exception as exc:
        if claim is None:
            raise
        await run_in_threadpool(
            store.release, principal, session_id, claim.attempt, request.state.request_id
        )
        if isinstance(exc, MuthError):
            raise
        logger.warning(
            "engine_failure request_id=%s exception_type=%s",
            request.state.request_id,
            type(exc).__name__,
        )
        raise MuthError(
            503, "engine_failure", "A verificação falhou; pode repetir a tentativa."
        ) from exc


@sessions.post("/{session_id}/review", response_model=SessionView)
async def review_session(
    request: Request, principal: PrincipalDep, session_id: str, decision: ReviewDecision
):
    return await run_in_threadpool(
        request.app.state.store.review, principal, session_id, decision, request.state.request_id
    )


@sessions.delete("/{session_id}", status_code=204)
async def delete_session(request: Request, principal: PrincipalDep, session_id: str):
    await run_in_threadpool(
        request.app.state.store.delete, principal, session_id, request.state.request_id
    )
    return Response(status_code=204)


@sessions.get("/{session_id}/events", response_model=list[AuditEvent])
async def session_events(
    request: Request,
    principal: PrincipalDep,
    session_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await run_in_threadpool(request.app.state.store.events, principal, session_id, limit)


@sessions.post("/{session_id}/feedback")
async def learning_feedback(
    request: Request, principal: PrincipalDep, session_id: str, feedback: LearningFeedback
):
    result = await run_in_threadpool(
        request.app.state.learning.feedback,
        principal,
        session_id,
        feedback,
        request.state.request_id,
    )
    # The periodic worker refines the next version; labelling never delays a verification.
    return result


@sessions.delete("/{session_id}/learning-consent", status_code=204)
async def withdraw_learning(request: Request, principal: PrincipalDep, session_id: str):
    await run_in_threadpool(
        request.app.state.learning.withdraw, principal, session_id, request.state.request_id
    )
    return Response(status_code=204)


@router.get("/learning", dependencies=[Depends(require_database)], tags=["Aprendizagem"])
async def learning_status(request: Request, principal: PrincipalDep):
    return await run_in_threadpool(request.app.state.learning.status, principal.tenant_id)


@router.post("/learning/refine", dependencies=[Depends(require_database)], tags=["Aprendizagem"])
async def refine(request: Request, principal: PrincipalDep):
    return await run_in_threadpool(request.app.state.learning.run, principal.tenant_id)


@router.post(
    "/learning/{role}/rollback", dependencies=[Depends(require_database)], tags=["Aprendizagem"]
)
async def rollback(
    request: Request,
    principal: PrincipalDep,
    role: Literal["face", "liveness"],
    policy_id: Annotated[str, Query(min_length=1, max_length=48)],
):
    result = await run_in_threadpool(
        request.app.state.learning.rollback,
        principal.tenant_id,
        role,
        policy_id,
        principal,
        request.state.request_id,
    )
    return result


@engines.post("/verifications", response_model=Verification, tags=["MUTH Verify"])
async def verify(
    request: Request, principal: PrincipalDep, document: ImageFile, selfie: ImageFile
) -> Verification:
    settings = request.app.state.settings
    document_image = await read_image(document, settings, capacity=request.app.state.capacity)
    selfie_image = await read_image(selfie, settings, capacity=request.app.state.capacity)
    service = await run_in_threadpool(current_service, request, principal)
    return await run_in_threadpool(
        invoke_engine, request, service.verify, document_image, selfie_image
    )


@engines.post("/faces/compare", response_model=Check, tags=["MUTH Face"])
async def compare(
    request: Request, principal: PrincipalDep, reference: ImageFile, selfie: ImageFile
) -> Check:
    settings = request.app.state.settings
    reference_image = await read_image(reference, settings, capacity=request.app.state.capacity)
    selfie_image = await read_image(selfie, settings, capacity=request.app.state.capacity)
    bundle = await run_in_threadpool(current_engines, request, principal)
    return await run_in_threadpool(
        invoke_engine, request, bundle.face.compare, reference_image, selfie_image
    )


@engines.post("/liveness", response_model=Check, tags=["MUTH Liveness"])
async def liveness(request: Request, principal: PrincipalDep, selfie: ImageFile) -> Check:
    image = await read_image(
        selfie, request.app.state.settings, capacity=request.app.state.capacity
    )
    bundle = await run_in_threadpool(current_engines, request, principal)
    return await run_in_threadpool(invoke_engine, request, bundle.liveness.assess, image)


@engines.post("/documents/analyze", response_model=DocumentCheck, tags=["MUTH ID"])
async def analyze(request: Request, document: ImageFile) -> DocumentCheck:
    image = await read_image(
        document, request.app.state.settings, capacity=request.app.state.capacity
    )
    analysis = await run_in_threadpool(
        invoke_engine, request, request.app.state.engines.document.analyze, image
    )
    return analysis.check


@router.post("/auth/authenticate", tags=["MUTH Auth"], status_code=501)
async def authenticate_identity() -> None:
    raise MuthError(501, "auth_not_implemented", "MUTH Auth ainda não implementado.")


router.include_router(sessions)
router.include_router(engines)
