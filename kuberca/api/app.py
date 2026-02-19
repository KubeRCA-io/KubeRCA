"""FastAPI application factory for KubeRCA.

Usage::

    from kuberca.api.app import create_app

    app = create_app(
        coordinator=coordinator,
        cache=cache,
        rule_engine=rule_engine,
        llm_analyzer=llm_analyzer,
        config=config,
    )

The factory is designed for use by both the production bootstrap
(``kuberca.app``) and unit tests.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from kuberca.api.routes import router
from kuberca.api.schemas import ErrorResponse

_log = structlog.get_logger(component="api.app")

_API_PREFIX = "/api/v1"


def create_app(
    coordinator: Any,
    cache: Any,
    rule_engine: Any = None,
    llm_analyzer: Any = None,
    config: Any = None,
    analysis_queue: Any = None,
) -> FastAPI:
    """Create and configure the KubeRCA FastAPI application.

    Args:
        coordinator:    AnalystCoordinator instance.
        cache:          ResourceCache instance.
        rule_engine:    Optional rule engine (used for metadata only).
        llm_analyzer:   Optional LLMAnalyzer (used for metadata only).
        config:         KubeRCAConfig.  Used for cluster_id and version metadata.
        analysis_queue: Optional AnalysisQueue for queue-full checks.

    Returns:
        Configured FastAPI application, ready to be served by uvicorn.
    """
    from kuberca import __version__

    cluster_id: str = ""
    if config is not None and hasattr(config, "cluster_id"):
        cluster_id = config.cluster_id or ""

    app = FastAPI(
        title="KubeRCA",
        summary="Kubernetes Root Cause Analysis API",
        version=__version__,
        description=(
            "KubeRCA provides deterministic root-cause analysis for Kubernetes "
            "incidents, combining a rule engine with an optional LLM analyser."
        ),
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
        openapi_url="/api/v1/openapi.json",
    )

    # Store dependencies in app.state so route handlers can access them
    # without module-level globals or complex DI frameworks.
    app.state.coordinator = coordinator
    app.state.cache = cache
    app.state.rule_engine = rule_engine
    app.state.llm_analyzer = llm_analyzer
    app.state.config = config
    app.state.cluster_id = cluster_id
    app.state.analysis_queue = analysis_queue

    # Mount the API router
    app.include_router(router, prefix=_API_PREFIX)

    # -----------------------------------------------------------------------
    # Exception handlers
    # -----------------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """Map Pydantic validation errors to our error envelope.

        Distinguishes between resource-format errors and time-window errors
        so callers get the correct error code.
        """
        errors = exc.errors()
        first_field = ""
        first_msg = ""
        if errors:
            locs = errors[0].get("loc", ())
            # locs is a tuple like ("body", "resource") or ("body", "time_window")
            first_field = str(locs[-1]) if locs else ""
            first_msg = str(errors[0].get("msg", ""))

        error_code = "INVALID_TIME_WINDOW" if "time_window" in first_field else "INVALID_RESOURCE_FORMAT"

        return JSONResponse(
            status_code=400,
            content=ErrorResponse(error=error_code, detail=first_msg).model_dump(),
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Catch-all for unhandled exceptions — never expose stack traces."""
        _log.error(
            "unhandled_exception",
            path=str(request.url.path),
            method=request.method,
            error=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error="INTERNAL_ERROR",
                detail="An unexpected error occurred.",
            ).model_dump(),
        )

    return app
