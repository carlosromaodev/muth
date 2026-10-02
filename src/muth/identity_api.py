"""Read, delete and compare a consented identity without exposing its template."""

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from muth.api import PrincipalDep, current_engines, invoke_engine, require_database
from muth.errors import MuthError
from muth.identity_models import IdentityView
from muth.identity_service import IdentityComparison
from muth.media import read_image
from muth.security import Principal, authenticate, require_scope

router = APIRouter(prefix="/identity-api/identities", tags=["Identidade guardada"])
operator_router = APIRouter(
    prefix="/v1", dependencies=[Depends(authenticate), Depends(require_database)]
)


@router.get("/{identity_id}", response_model=IdentityView)
async def get_identity(request: Request, identity_id: str):
    return await run_in_threadpool(
        request.app.state.identities.get, request.state.identity_principal, identity_id
    )


@router.delete("/{identity_id}", status_code=204)
async def delete_identity(request: Request, identity_id: str):
    await run_in_threadpool(
        request.app.state.identities.delete,
        request.state.identity_principal,
        identity_id,
        request.state.request_id,
    )
    return Response(status_code=204)


async def compare_identity(request, principal, identity_id):
    async with request.form(
        max_files=1, max_fields=0, max_part_size=request.app.state.settings.max_upload_bytes
    ) as form:
        if (
            list(form.keys()) != ["selfie"]
            or len(form.multi_items()) != 1
            or not isinstance(form["selfie"], UploadFile)
        ):
            raise MuthError(422, "identity_selfie_required", "Envie apenas uma selfie.")
        selfie = await read_image(
            form["selfie"], request.app.state.settings, capacity=request.app.state.capacity
        )
    template, fingerprint = await run_in_threadpool(
        request.app.state.identities.template, principal, identity_id
    )
    bundle = await run_in_threadpool(current_engines, request, principal)
    return await run_in_threadpool(
        invoke_engine,
        request,
        request.app.state.identity_service.compare,
        identity_id,
        template,
        fingerprint,
        bundle,
        selfie,
    )


@router.post("/{identity_id}/compare", response_model=IdentityComparison)
async def compare(request: Request, identity_id: str):
    return await compare_identity(request, request.state.identity_principal, identity_id)


@operator_router.get("/capture-identities/{identity_id}", response_model=IdentityView)
async def operator_identity(request: Request, principal: PrincipalDep, identity_id: str):
    require_scope(principal, "capture_review")
    actor = Principal(
        request.app.state.settings.capture_tenant_id, principal.key_id, principal.scopes
    )
    return await run_in_threadpool(request.app.state.identities.get, actor, identity_id)


@operator_router.delete("/capture-identities/{identity_id}", status_code=204)
async def operator_delete(request: Request, principal: PrincipalDep, identity_id: str):
    require_scope(principal, "capture_review")
    actor = Principal(
        request.app.state.settings.capture_tenant_id, principal.key_id, principal.scopes
    )
    await run_in_threadpool(
        request.app.state.identities.delete, actor, identity_id, request.state.request_id
    )
    return Response(status_code=204)


@operator_router.post("/auth/authenticate", response_model=IdentityComparison, tags=["MUTH Auth"])
async def authenticate_profile(
    request: Request,
    principal: PrincipalDep,
    identity_id: str = Query(pattern=r"^mth_idn_[a-f0-9]{32}$"),
):
    require_scope(principal, "capture_review")
    if request.app.state.settings.engine_mode != "biometric":
        raise MuthError(
            503, "identity_engine_unavailable", "A comparação biométrica está indisponível."
        )
    actor = Principal(
        request.app.state.settings.capture_tenant_id, principal.key_id, principal.scopes
    )
    await run_in_threadpool(request.app.state.identities.get, actor, identity_id)
    return await compare_identity(request, actor, identity_id)
