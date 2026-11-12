"""App layer: FastAPI app factory, routers under /api, CORS, scheduler, demo seed and the built frontend."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import catalog, seed
from app.agent.loop import make_llm
from app.api import (Services, agent, approvals, broker_info, health, orders, payout, portfolio, products, profile,
                     webhooks)
from app.brokerage import make_broker
from app.channel3 import Channel3Client
from app.config import Settings, get_settings
from app.db import init_db, make_engine, make_session_factory
from app.paypal.client import PayPalClient
from app.scheduler import start_scheduler

logger = logging.getLogger(__name__)
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def build_services(settings: Settings) -> Services:
    engine = make_engine(settings.database_url)
    init_db(engine)
    return Services(settings=settings, session_factory=make_session_factory(engine), broker=make_broker(settings),
                    llm=make_llm(settings), paypal=PayPalClient(settings), channel3=Channel3Client(settings))


def create_app(services: Services | None = None, enable_scheduler: bool = True,
               frontend_dist: Path | None = FRONTEND_DIST) -> FastAPI:
    settings = services.settings if services else get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    services = services or build_services(settings)
    kind, label, _ = broker_info(services)
    logger.info("Broker in use: %s (%s)", kind, label)
    if settings.broker_mode.lower() == "alpaca" and kind != "alpaca":
        logger.warning("BROKER_MODE=alpaca but Alpaca keys are missing/placeholder: using the SIMULATED broker, "
                       "so nothing will appear in your Alpaca dashboard")

    with services.session_factory() as session:                      # products always; demo history only if asked
        catalog.ensure_products(session)
        if settings.seed_on_start and seed.seed_demo(session, services.broker, settings):
            logger.info("Demo data seeded (SEED_ON_START=true)")
        session.commit()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = start_scheduler(services) if enable_scheduler else None
        yield
        if scheduler:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Penny", version="2.0.0", lifespan=lifespan,
                  description="Cashback micro-investing agent (PayPal sandbox + paper trading). "
                              "Simulated cashback, paper trading, not financial advice.")
    app.state.services = services
    app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_methods=["*"], allow_headers=["*"])
    for module in (health, products, orders, portfolio, profile, agent, approvals, payout):
        app.include_router(module.router, prefix="/api")
    app.include_router(webhooks.router)                              # PayPal calls this one: /webhooks/paypal

    if frontend_dist and (frontend_dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            if path.startswith(("api/", "webhooks/")):
                raise HTTPException(404, "not found")
            candidate = (frontend_dist / path).resolve()
            if path and candidate.is_file() and frontend_dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")        # client-side routes fall back to the app
    else:
        @app.get("/", include_in_schema=False)
        def no_frontend() -> JSONResponse:
            return JSONResponse({"message": "Frontend not built. Run `cd frontend && npm run build`, "
                                             "or start the dev server (npm run dev) on http://localhost:5173.",
                                 "api_docs": "/docs"})
    return app
