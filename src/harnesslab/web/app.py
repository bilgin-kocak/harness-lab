"""FastAPI application factory for the local dashboard."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import harnesslab
from harnesslab.config import Settings
from harnesslab.storage.database import Database
from harnesslab.storage.repository import Repository
from harnesslab.web.filters import FILTERS
from harnesslab.web.routes import router

WEB_DIR = Path(__file__).resolve().parent


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    db = db or Database(settings.resolved_database_url)
    db.create_all()

    app = FastAPI(title="Harness Lab", version=harnesslab.__version__, docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.autoescape = True
    templates.env.filters.update(FILTERS)
    templates.env.globals["app_version"] = harnesslab.__version__

    app.state.settings = settings
    app.state.db = db
    app.state.repo = Repository(db, settings.home)
    app.state.templates = templates

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(router)
    return app
