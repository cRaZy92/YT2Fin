from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import PlainTextResponse

from app import organizer, ytdlp_client
from app import settings as settings_mod
from app.config import AppConfig, load_config
from app.db import init_db, make_engine, make_session_factory
from app.embedded import Stripper
from app.events import EventBus
from app.logbuffer import LogBuffer, setup_logging
from app.notify import Notifier
from app.scheduler import Scheduler
from app.sync import Syncer
from app.templating import templates
from app.timeutil import utcnow
from app.updater import Updater
from app.web import dashboard, mappings, settings_page, sse, videos
from app.worker import Worker

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
RESTART_EXIT_CODE = 3
PUBLIC_PATHS = {"/healthz"}


class BasicAuthMiddleware:
    """Pure ASGI middleware so streaming (SSE) responses are not buffered."""

    def __init__(self, app, username: str, password: str) -> None:
        self.app = app
        self._username = username.encode()
        self._password = password.encode()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        response = PlainTextResponse(
            "Authentication required", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="yt-jellyfin-sync"'},
        )
        await response(scope, receive, send)

    def _authorized(self, scope) -> bool:
        header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic":
            return False
        try:
            username, _, password = base64.b64decode(encoded, validate=True).partition(b":")
        except (binascii.Error, ValueError):
            return False
        return secrets.compare_digest(username, self._username) & secrets.compare_digest(password, self._password)


class SameOriginMiddleware:
    """Rejects state-changing requests that another website triggers in the user's browser (CSRF)."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] not in self.SAFE_METHODS and not self._same_origin(scope):
            await PlainTextResponse("Cross-site request blocked", status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _same_origin(scope) -> bool:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        if headers.get("sec-fetch-site") == "cross-site":
            return False
        origin = headers.get("origin")
        if origin is None:
            return True  # non-browser clients and older browsers on same-origin form posts
        allowed = {headers.get("host"), headers.get("x-forwarded-host")} - {None}
        return urlsplit(origin).netloc in allowed


def request_restart(app: FastAPI) -> None:
    app.state.exit_code = RESTART_EXIT_CODE
    server = app.state.server
    if server is not None:
        server.should_exit = True


def create_app(config: AppConfig | None = None, *, start_background: bool = True) -> FastAPI:
    config = config or load_config()
    config.config_dir.mkdir(parents=True, exist_ok=True)
    log_buffer = LogBuffer()
    setup_logging(config, log_buffer)  # before migrating, so migrations and backups are logged
    engine = make_engine(config.db_path)
    init_db(engine, config.config_dir / "backups")
    session_factory = make_session_factory(engine)
    bus = EventBus()
    notifier = Notifier(bus, templates.env)
    log_buffer.listener = notifier.log_line
    ytdlp_client.configure(pot_provider_url=config.pot_provider_url)
    worker = Worker(session_factory, config, notifier)
    syncer = Syncer(session_factory, config, worker, notifier)
    updater = Updater(worker)
    stripper = Stripper(session_factory, config.media_root)
    scheduler = Scheduler(session_factory, worker, syncer, updater)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.bind(asyncio.get_running_loop())
        try:
            with session_factory() as session:
                organizer.reconcile(session, config.media_root, config.staging_dir,
                                    settings_mod.get_all(session)["max_filename_length"])
        except Exception:
            log.exception("Startup reconciliation failed")
        tasks: list[asyncio.Task] = []
        if start_background:
            worker.on_idle = lambda: request_restart(app)
            # Don't start a download while the startup yt-dlp self-update may be swapping modules.
            worker.hold_until = utcnow() + timedelta(minutes=5)
            tasks = [asyncio.create_task(worker.run()), asyncio.create_task(scheduler.run())]
        log.info("yt-jellyfin-sync started (media=%s, config=%s)", config.media_root, config.config_dir)
        try:
            yield
        finally:
            app.state.stopping = True
            await worker.stop()
            for task in [*tasks, *app.state.background]:
                task.cancel()
            await asyncio.gather(*tasks, *app.state.background, return_exceptions=True)
            log_buffer.listener = None
            engine.dispose()

    app = FastAPI(title="YouTube → Jellyfin Sync", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.session_factory = session_factory
    app.state.bus = bus
    app.state.notifier = notifier
    app.state.worker = worker
    app.state.syncer = syncer
    app.state.updater = updater
    app.state.stripper = stripper
    app.state.scheduler = scheduler
    app.state.log_buffer = log_buffer
    app.state.exit_code = 0
    app.state.server = None
    app.state.stopping = False
    app.state.background = set()
    app.add_middleware(SameOriginMiddleware)
    if config.auth_user and config.auth_password:
        app.add_middleware(BasicAuthMiddleware, username=config.auth_user, password=config.auth_password)
    @app.exception_handler(StarletteHTTPException)
    async def http_error_as_text(request, exc: StarletteHTTPException):
        # Plain text so htmx error toasts read "Folder not found", not {"detail": ...}
        return PlainTextResponse(str(exc.detail), status_code=exc.status_code, headers=exc.headers)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for module in (dashboard, sse, mappings, videos, settings_page):
        app.include_router(module.router)
    return app
