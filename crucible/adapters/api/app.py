"""The /v1 application."""

from __future__ import annotations

from fastapi import FastAPI

from crucible import __version__
from crucible.adapters.api.deps import AppContext
from crucible.adapters.api.problems import install_problem_handlers
from crucible.adapters.api.routers import records, supervision, tasks

API_PREFIX = "/v1"


def create_app(ctx: AppContext) -> FastAPI:
    app = FastAPI(
        title="Crucible",
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )
    app.state.ctx = ctx
    install_problem_handlers(app)
    app.include_router(supervision.router, prefix=API_PREFIX)
    app.include_router(tasks.router, prefix=API_PREFIX)
    app.include_router(records.router, prefix=API_PREFIX)
    return app
