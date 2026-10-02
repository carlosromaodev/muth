import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

from muth.api import router
from muth.capacity import InferenceCapacity
from muth.capture_api import router as capture_router
from muth.capture_security import CaptureAccess, CaptureRateLimiter
from muth.capture_storage import CaptureStore
from muth.config import Settings
from muth.engines.biometric import BiometricRuntime
from muth.engines.bundle import EngineBundle
from muth.errors import MuthError
from muth.learning import LearningService, refinement_loop
from muth.middleware import (
    WEB_SECURITY_HEADERS,
    Metrics,
    PlatformMiddleware,
    RateLimiter,
    error_response,
)
from muth.services.verify import VerifyService
from muth.storage import Database, SessionStore


def create_app(settings: Settings | None = None, engines: EngineBundle | None = None) -> FastAPI:
    settings = settings if settings is not None else Settings()
    database = Database(settings)
    runtime = None
    if settings.engine_mode == "biometric":
        if not settings.biometric_manifest:
            raise ValueError("Modo biométrico exige MUTH_BIOMETRIC_MANIFEST.")
        runtime = BiometricRuntime(Path(settings.biometric_manifest))

    @asynccontextmanager
    async def lifespan(app):
        tenants = {key.tenant_id for key in settings.tenants}
        if settings.api_key.get_secret_value():
            tenants.add("local")
        task = (
            asyncio.create_task(
                refinement_loop(app.state.learning, settings.learning_interval_seconds, tenants)
            )
            if runtime and settings.learning_enabled
            else None
        )

        async def capture_retention_loop():
            while True:
                try:
                    if await run_in_threadpool(database.ready):
                        await run_in_threadpool(app.state.capture_store.purge)
                except Exception as exc:
                    logging.getLogger("muth.capture").warning(
                        "capture_purge_failed exception_type=%s", type(exc).__name__
                    )
                await asyncio.sleep(300)

        retention_task = asyncio.create_task(capture_retention_loop())
        try:
            yield
        finally:
            retention_task.cancel()
            with suppress(asyncio.CancelledError):
                await retention_task
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            database.engine.dispose()

    app = FastAPI(
        title="MUTH API",
        version="0.5.0",
        lifespan=lifespan,
        description=(
            "Infraestrutura africana de identidade digital. Sessões B2B com consentimento, "
            "isolamento por empresa e resultados cifrados. Motores demo não verificam identidades."
        ),
    )
    app.state.settings = settings
    app.state.database = database
    app.state.store = SessionStore(database, settings)
    app.state.capture_access = CaptureAccess(app.state.store)
    app.state.capture_store = CaptureStore(app.state.store)
    app.state.capture_limiter = CaptureRateLimiter(settings, app.state.store.fingerprint_key)
    app.state.runtime = runtime
    app.state.learning = LearningService(app.state.store, runtime)
    app.state.engines = engines or (runtime.bundle() if runtime else EngineBundle.demo())
    app.state.verify_service = VerifyService(
        app.state.engines.face,
        app.state.engines.liveness,
        app.state.engines.document,
        demo=settings.engine_mode != "biometric",
    )
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.metrics = Metrics()
    app.state.capacity = InferenceCapacity(settings.max_inference_requests, app.state.metrics)
    app.add_middleware(
        PlatformMiddleware,
        settings=settings,
        metrics=app.state.metrics,
        capacity=app.state.capacity,
    )
    app.include_router(router)
    app.include_router(capture_router)
    web = Path(__file__).parent / "web"
    app.mount("/assets", StaticFiles(directory=web, check_dir=False), name="capture-assets")

    @app.get("/", include_in_schema=False)
    @app.get("/capture", include_in_schema=False)
    def capture_page():
        return FileResponse(
            web / "index.html",
            headers=WEB_SECURITY_HEADERS,
        )

    @app.exception_handler(MuthError)
    async def domain_error(request: Request, exc: MuthError):
        return error_response(exc.status, exc.code, exc.message, request.state.request_id)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        codes = {413: "image_too_large", 415: "unsupported_media", 422: "invalid_image"}
        return error_response(
            exc.status_code,
            codes.get(exc.status_code, "http_error"),
            str(exc.detail),
            request.state.request_id,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(
            422,
            "invalid_request",
            "Pedido inválido. Verifique os campos exigidos.",
            request.state.request_id,
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        return error_response(
            500,
            "internal_error",
            "Não foi possível concluir o pedido.",
            getattr(request.state, "request_id", "unavailable"),
        )

    @app.get("/health", tags=["Sistema"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "muth", "version": "0.5.0"}

    @app.get("/health/ready", tags=["Sistema"])
    def ready() -> JSONResponse:
        db_ready = database.ready()
        demo = settings.engine_mode == "demo"
        available = demo or runtime is not None
        return JSONResponse(
            status_code=200 if available and db_ready else 503,
            content={
                "status": ("demo" if demo else "biometric_research")
                if available and db_ready
                else "not_ready",
                "mode": settings.engine_mode,
                "database_ready": db_ready,
                "identity_verification_ready": False,
                "biometric_inference_ready": runtime is not None and db_ready,
                "permitted_use": runtime.manifest.permitted_use if runtime else None,
            },
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(
            generate_latest(app.state.metrics.registry),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )

    return app
