from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from env.db import init_db
from env.erp.router import router as erp_router
from env.fileshare.router import router as fileshare_router
from env.mailbox.router import router as mailbox_router
from env.seed.generator import DATA_DIR, seed_company_data
from env.ticketing.router import router as ticketing_router
from env.truth.router import router as truth_router
from env.vendor_portal.router import router as portal_router
from ui.router import router as ui_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Ensure database and seed data exist on startup."""
    init_db()
    # If no invoices exist or data dir is empty, run initial seed
    if not os.path.exists(os.path.join(DATA_DIR, "invoices")):
        seed_company_data()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="CentrAlign Mock Company Environment",
        description="Unified multi-system environment hosting ERP, Vendor Portal, Mailbox, Fileshare, and Ticketing.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Mount all sub-systems
    app.include_router(ui_router)
    app.include_router(portal_router)
    app.include_router(erp_router)
    app.include_router(mailbox_router)
    app.include_router(fileshare_router)
    app.include_router(ticketing_router)
    app.include_router(truth_router)

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse(url="/ui")

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.getenv("MOCK_PORT", "8001"))
    host = os.getenv("MOCK_HOST", "0.0.0.0")
    print(f"Starting CentrAlign Mock Company Environment on http://localhost:{port}")
    uvicorn.run("env.server:app", host=host, port=port, reload=False)
