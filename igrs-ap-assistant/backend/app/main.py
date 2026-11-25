"""
FastAPI Application — IGRS AP Legal & Duty Intelligence Platform.

Phase 1: Deterministic calculator API endpoints.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.app.db.models import create_tables, get_engine, get_session_factory
from backend.app.db.seed import seed_all
from backend.app.core.config import (
    APP_ENV,
    CORS_ALLOWED_ORIGINS,
    DATABASE_URL,
    PROJECT_ROOT,
    SEEDS_DIR,
    validate_runtime_config,
)
from backend.app.api.calculator import router as calc_router
from backend.app.api.admin import router as admin_router


# Global engine and session factory
engine = None
SessionFactory = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, SessionFactory
    validate_runtime_config()
    engine = get_engine(DATABASE_URL)
    create_tables(engine)
    SessionFactory = get_session_factory(engine)

    # Seed data on startup if tables are empty
    session = SessionFactory()
    try:
        from backend.app.db.models import DutyRate
        count = session.query(DutyRate).count()
        if count == 0 and APP_ENV != "production":
            seed_all(session, SEEDS_DIR)
        # Do not seed illustrative prohibited-property examples into the app DB.
    finally:
        session.close()

    yield  # App runs here

    if engine:
        engine.dispose()


app = FastAPI(
    title="AP IGRS Assistant — Local Prototype",
    description=(
        "Independent prototype for exploring deterministic calculations and document search. "
        "Sample data and legal interpretations are unverified; not for official use."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(calc_router, prefix="/api", tags=["Calculator"])
app.include_router(admin_router, prefix="/api", tags=["Admin"])


@app.get("/health")
def health_check():
    return {"status": "ok", "version": "0.1.0"}


# Mount static frontend at the root (must be after all explicit API routes)
frontend_dir = PROJECT_ROOT / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
