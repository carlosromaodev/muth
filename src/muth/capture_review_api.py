"""Operator-only review of volunteered browser contributions."""

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool

from muth.api import PrincipalDep, require_database
from muth.capture_learning import CaptureLearningInfo, CaptureLearningReview
from muth.errors import MuthError
from muth.security import authenticate, require_scope

router = APIRouter(
    prefix="/v1/capture-learning",
    tags=["Revisão de contribuições"],
    dependencies=[Depends(authenticate), Depends(require_database)],
)


@router.get("")
async def queue(
    request: Request, principal: PrincipalDep, limit: int = Query(default=50, ge=1, le=100)
):
    require_scope(principal, "capture_review")
    return await run_in_threadpool(request.app.state.capture_learning.queue, principal, limit)


@router.post("/{session_id}/review", response_model=CaptureLearningInfo)
async def review(
    request: Request, principal: PrincipalDep, session_id: str, payload: CaptureLearningReview
):
    require_scope(principal, "capture_review")
    return await run_in_threadpool(
        request.app.state.capture_learning.review,
        principal,
        session_id,
        payload,
        request.state.request_id,
    )


@router.get("/status")
async def status(request: Request, principal: PrincipalDep):
    require_scope(principal, "capture_review")
    return await run_in_threadpool(
        request.app.state.learning.status, request.app.state.settings.capture_tenant_id
    )


@router.post("/refine")
async def refine(request: Request, principal: PrincipalDep):
    require_scope(principal, "capture_review")
    settings = request.app.state.settings
    if not settings.learning_enabled or not settings.capture_learning_enabled:
        raise MuthError(409, "learning_disabled", "Aprendizagem desactivada.")
    return await run_in_threadpool(request.app.state.learning.run, settings.capture_tenant_id)
