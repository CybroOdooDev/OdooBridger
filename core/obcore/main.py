"""OdooBridger core: accounts, SSO, billing, leads and the staff console."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from obcore import keys
from obcore.api import auth, billing_hook, portal, public, sso, staff
from obcore.config import settings
from obcore.db import create_schema, session_scope

logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def _check_production() -> None:
    if not settings.is_production:
        return
    if "dev-only" in settings.jwt_secret:
        raise RuntimeError("JWT_SECRET must be set in production")
    if "dev-only" in settings.master_encryption_key:
        raise RuntimeError("MASTER_ENCRYPTION_KEY must be set in production")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_production()
    # Migrations: builds, upgrades or adopts the database (obcore/migrations.py).
    create_schema()
    with session_scope() as db:
        from obcore.services.jobs import ensure_static_staff

        from obcore.services.catalog import ensure_biometric, retire_demo

        ensure_static_staff(db)
        ensure_biometric(db)
        retire_demo(db)
        keys.load(db)
    tasks = [asyncio.create_task(_jobs()), asyncio.create_task(_alerts())] if settings.run_background_jobs else []
    log.info("%s core started (env=%s)", settings.app_name, settings.environment)
    yield
    for task in tasks:
        task.cancel()


async def _alerts() -> None:
    from obcore.services.jobs import alert_loop

    await asyncio.sleep(20)
    await alert_loop()


async def _jobs() -> None:
    from obcore.services.jobs import loop

    await asyncio.sleep(5)
    await loop()


app = FastAPI(title="OdooBridger Core", version="1.0.0", lifespan=lifespan, docs_url="/docs")
if settings.cors_origin_list:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

api = APIRouter(prefix="/api/v1")
for module in (auth, public, portal, sso, staff, billing_hook):
    api.include_router(module.router)
app.include_router(api)


@app.get("/.well-known/jwks.json", include_in_schema=False)
def jwks() -> JSONResponse:
    return JSONResponse(keys.jwks(), headers={"Cache-Control": "public, max-age=300"})


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok", "service": "odoobridger-core", "version": app.version}


class Revalidating(StaticFiles):
    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


STATIC = Path(__file__).parent / "static"
app.mount("/app", Revalidating(directory=STATIC / "portal", html=True), name="portal")
app.mount("/staff", Revalidating(directory=STATIC / "staff", html=True), name="staff")
app.mount("/assets", Revalidating(directory=STATIC / "shared"), name="shared")
app.mount("/site", Revalidating(directory=STATIC / "site", html=True), name="site")


@app.get("/", include_in_schema=False)
def root():
    """The website's home page (the site's other pages are under /site/)."""
    return FileResponse(STATIC / "site" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)
