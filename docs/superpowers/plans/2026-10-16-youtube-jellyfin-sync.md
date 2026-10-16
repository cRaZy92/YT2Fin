# YouTube → Jellyfin Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single-container web service that syncs YouTube playlists (and manual lists) into Jellyfin-ready folders. Files are named for Jellyfin "Shows" and "Music Videos" libraries. The UI shows progress in real time.

**Architecture:** One Python process. FastAPI serves a Jinja + htmx UI and an SSE stream. SQLite is the source of truth for mappings, order and names. An `organizer` module makes the disk match the DB through collision-safe renames. An asyncio worker runs yt-dlp downloads in threads with throttling, retries and a global rate-limit cool-down. An asyncio scheduler runs staggered playlist syncs and the daily yt-dlp self-update.

**Tech Stack:** Python 3.12, FastAPI, Starlette, Jinja2, SQLAlchemy 2 + SQLite, yt-dlp (library, `[default]` extra) + `deno` (pip), ffmpeg, htmx 2.0.4 + htmx-ext-sse 2.2.2 + SortableJS 1.15.6 (vendored, no build step), pytest + pytest-asyncio + httpx.

**Spec:** `docs/superpowers/specs/2026-10-16-youtube-jellyfin-sync-design.md`

## Global Constraints

- Python `>=3.12`; container base `python:3.12-slim`. Local dev venv: `uv venv --python 3.12 .venv`.
- Volumes/paths: `/config` (DB `app.db`, `cookies.txt`, `logs/`), `/media` (shared with Jellyfin). Staging dir is `/media/.ytjs-tmp`. In code, always derive these from `AppConfig`, never hard-code them.
- Env: `PUID`, `PGID`, `TZ`, `PORT` (default 8080), optional `AUTH_USER` + `AUTH_PASSWORD` (HTTP basic; no auth when unset), `MEDIA_ROOT` (default `/media`), `CONFIG_DIR` (default `/config`).
- Show file layout: `<mapping folder>/Season NN/<Show> SxxEyy <Title>.<ext>` + `.nfo` + `-thumb.jpg`; `tvshow.nfo` in the mapping folder.
- Music video layout: flat `<mapping folder>/<Artist> - <Song>.<ext>` + `.nfo` + `-thumb.jpg`; duplicates get ` (2)`, ` (3)` in position order.
- Forbidden filename chars `< > : " / \ | ? *` and control chars are replaced; default max filename length 180.
- Sync is **additive only**: never delete or reorder because of the playlist; ignored ids are never re-added by sync.
- Episode numbering uses all videos in position order **except `unavailable` ones**. Pending and failed videos reserve their number.
- All DB writes and organizer (filesystem) calls happen on the asyncio event-loop thread. Only yt-dlp network/download calls run in worker threads (`asyncio.to_thread`).
- Defaults: delay between downloads 30–90 s, `sleep_requests` 1 s, concurrency 1, cool-down 60 min doubling up to 720 min, retry backoff `[10, 60, 360]` minutes, sync interval 360 min, sync jitter 300 s.
- Process exit code `3` means "restart me" (used after a yt-dlp update); the entrypoint loop restarts on 3 and exits on anything else.
- Times are stored as naive UTC (`app.timeutil.utcnow()`); convert to local only in templates.

## Review Focus

1. **Hostile titles** (emoji, CJK, only forbidden chars such as `???`, 300-char titles): filenames must be valid, non-empty, unique and must keep the `SxxEyy` code. Test: `tests/test_naming.py::test_hostile_titles` (Task 3).
2. **Folder input escaping `/media`** (`../../etc`, absolute `/etc`, the staging dir, the root itself): the mapping must be rejected and nothing created. Test: `tests/test_services.py::test_create_mapping_rejects_escaping_folders` (Task 9).
3. **Video or whole mapping deleted while its download is running**: no orphan files in the media folder, no crash, staging cleaned. Test: `tests/test_worker.py::test_mapping_deleted_mid_download_leaves_no_files` (Task 12).
4. **Playlist with duplicates and `[Private video]`/`[Deleted video]` placeholders**: each real video is added once and placeholders are skipped. Tests: `tests/test_ytdlp_client.py::test_entries_skip_placeholders_and_tabs` (Task 8), `tests/test_sync.py::test_add_entries_dedupes_within_batch` (Task 11).
5. **Foreign file already sitting at a rename target**: never overwrite it; skip that video's rename and every rename chained on it. Test: `tests/test_organizer.py::test_apply_never_overwrites_foreign_file` (Task 6).

---

## File Structure

```
pyproject.toml                 # deps, pytest config
.gitignore
Dockerfile
docker-compose.yml             # example alongside Jellyfin
docker/entrypoint.sh           # PUID/PGID drop + restart loop
README.md
app/
  __init__.py
  __main__.py                  # uvicorn.Server runner, exits with app.state.exit_code
  config.py                    # AppConfig + load_config()
  timeutil.py                  # utcnow(), to_local()
  db.py                        # Base, make_engine, make_session_factory, init_db
  models.py                    # Mapping, Video, SeasonBreak, IgnoredVideo, Setting, enums
  settings.py                  # DEFAULTS, coerce, get_all, set_many, resolve, coerce_overrides
  naming.py                    # pure: sanitize, NamingItem, season_episode, compute_stems
  mvparse.py                   # pure: parse_artist_song, strip_noise, clean_channel
  nfo.py                       # pure: tvshow_nfo, episode_nfo, musicvideo_nfo
  organizer.py                 # disk <- DB: apply, place, delete_video_files, reconcile
  errors.py                    # ErrorKind, classify
  ytdlp_client.py              # yt-dlp wrapper: options, fetch_entries, download
  services.py                  # mapping/video mutations used by the web layer
  activity.py                  # ActiveItem, ActivitySnapshot dataclasses
  events.py                    # EventBus, Event, format_sse
  logbuffer.py                 # LogBuffer handler + setup_logging
  templating.py                # Jinja2Templates instance + filters
  notify.py                    # Notifier: renders fragments, publishes events
  sync.py                      # add_entries, Syncer
  worker.py                    # download queue
  updater.py                   # yt-dlp self-update
  scheduler.py                 # periodic syncs + update checks
  main.py                      # create_app, lifespan, auth middleware, request_restart
  web/
    __init__.py
    deps.py                    # get_session, render helpers
    dashboard.py               # /, cards, activity, queue pause/resume, logs, healthz
    sse.py                     # /events
    mappings.py                # new/edit/delete/sync, folder picker
    videos.py                  # detail page, list, reorder, rename, season, delete, retry, add
    settings_page.py           # settings, cookies, yt-dlp update
  templates/
    base.html, dashboard.html, mapping_form.html, mapping_detail.html, settings.html, logs.html
    partials/ progress.html, activity.html, log_line.html, mapping_card.html, folder_picker.html,
              video_list.html, video_row.html, video_edit_row.html, video_status.html
  static/
    app.css, app.js
    vendor/ htmx.min.js, sse.js, Sortable.min.js
tests/
  conftest.py, test_models.py, test_settings.py, test_naming.py, test_mvparse.py, test_nfo.py,
  test_organizer.py, test_organizer_place.py, test_errors.py, test_ytdlp_client.py,
  test_services.py, test_events.py, test_sync.py, test_worker.py, test_scheduler.py,
  test_web_dashboard.py, test_web_mappings.py, test_web_videos.py, test_web_settings.py,
  test_integration.py
```

All commands below run from the repo root: `/home/adam/Documents/Programming/youtube-jellyfin-sync`.

---

### Task 1: Project scaffold, config, DB and models

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `app/__init__.py`, `app/config.py`, `app/timeutil.py`, `app/db.py`, `app/models.py`
- Test: `tests/__init__.py` (empty), `tests/conftest.py`, `tests/test_models.py`

**Interfaces:**
- Produces:
  - `AppConfig(media_root: Path, config_dir: Path, auth_user: str|None=None, auth_password: str|None=None, port: int=8080)` with properties `db_path`, `cookies_path`, `logs_dir`, `staging_dir` and method `cookiefile() -> Path|None`; `load_config(env=os.environ) -> AppConfig`
  - `utcnow() -> datetime` (naive UTC), `to_local(dt) -> datetime|None`
  - `Base`, `make_engine(db_path: Path) -> Engine`, `make_session_factory(engine) -> sessionmaker[Session]` (`expire_on_commit=False`), `init_db(engine) -> None`
  - Models: `MappingType` (`SHOW="show"`, `MUSIC_VIDEO="music_video"`), `VideoStatus` (`PENDING, DOWNLOADING, DONE, FAILED, UNAVAILABLE`), `Mapping`, `Video` (with `display_title` property and `user_edited` flag), `SeasonBreak`, `IgnoredVideo`, `Setting`
  - Test fixtures: `config`, `session_factory`, `session`, `make_mapping`, `add_video`, `FakeNotifier`, `Clock`

- [ ] **Step 1: Create project files and dev environment**

`pyproject.toml`:
```toml
[project]
name = "yt-jellyfin-sync"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "jinja2>=3.1",
  "python-multipart>=0.0.12",
  "sqlalchemy>=2.0.36",
  "yt-dlp[default]>=2025.11.12",
  "deno>=2.0",
]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "httpx>=0.27"]

[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["app*"]

[tool.setuptools.package-data]
app = ["templates/*.html", "templates/partials/*.html", "static/*", "static/vendor/*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
markers = ["integration: downloads from real YouTube (run with -m integration)"]
addopts = "-m 'not integration'"
```

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
build/
/config/
/media/
```

`app/__init__.py` and `tests/__init__.py`: empty files.

Run:
```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -e '.[dev]'
```
Expected: installs without errors.

- [ ] **Step 2: Write `app/config.py` and `app/timeutil.py`**

`app/config.py`:
```python
from __future__ import annotations

import os
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    media_root: Path
    config_dir: Path
    auth_user: str | None = None
    auth_password: str | None = None
    port: int = 8080

    @property
    def db_path(self) -> Path:
        return self.config_dir / "app.db"

    @property
    def cookies_path(self) -> Path:
        return self.config_dir / "cookies.txt"

    @property
    def logs_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def staging_dir(self) -> Path:
        return self.media_root / ".ytjs-tmp"

    def cookiefile(self) -> Path | None:
        return self.cookies_path if self.cookies_path.is_file() else None


def load_config(env: MappingABC[str, str] = os.environ) -> AppConfig:
    return AppConfig(
        media_root=Path(env.get("MEDIA_ROOT", "/media")),
        config_dir=Path(env.get("CONFIG_DIR", "/config")),
        auth_user=env.get("AUTH_USER") or None,
        auth_password=env.get("AUTH_PASSWORD") or None,
        port=int(env.get("PORT", "8080")),
    )
```

`app/timeutil.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC timestamp; everything stored in the DB uses this."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone()
```

- [ ] **Step 3: Write `app/db.py`**

```python
from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(engine)
```

- [ ] **Step 4: Write `app/models.py`**

```python
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.timeutil import utcnow


class MappingType(StrEnum):
    SHOW = "show"
    MUSIC_VIDEO = "music_video"


class VideoStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class Mapping(Base):
    __tablename__ = "mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(20))
    folder: Mapped[str] = mapped_column(String(1000), unique=True)
    playlist_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    sync_interval_minutes: Mapped[int | None] = mapped_column(default=None)
    last_synced_at: Mapped[datetime | None] = mapped_column(default=None)
    last_sync_error: Mapped[str | None] = mapped_column(Text, default=None)
    show_title: Mapped[str | None] = mapped_column(String(300), default=None)
    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    videos: Mapped[list[Video]] = relationship(
        back_populates="mapping", order_by="Video.position", cascade="all, delete-orphan"
    )
    season_breaks: Mapped[list[SeasonBreak]] = relationship(cascade="all, delete-orphan")
    ignored: Mapped[list[IgnoredVideo]] = relationship(
        cascade="all, delete-orphan", order_by="IgnoredVideo.ignored_at"
    )


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (UniqueConstraint("mapping_id", "youtube_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mappings.id", ondelete="CASCADE"), index=True)
    youtube_id: Mapped[str] = mapped_column(String(32))
    yt_title: Mapped[str] = mapped_column(String(500), default="")
    channel: Mapped[str | None] = mapped_column(String(300), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    upload_date: Mapped[str | None] = mapped_column(String(8), default=None)
    duration: Mapped[int | None] = mapped_column(default=None)
    thumbnail_url: Mapped[str | None] = mapped_column(String(1000), default=None)
    yt_artist: Mapped[str | None] = mapped_column(String(300), default=None)
    yt_track: Mapped[str | None] = mapped_column(String(300), default=None)
    position: Mapped[int] = mapped_column()
    custom_title: Mapped[str | None] = mapped_column(String(500), default=None)
    artist: Mapped[str | None] = mapped_column(String(300), default=None)
    song: Mapped[str | None] = mapped_column(String(500), default=None)
    user_edited: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default=VideoStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    attempts: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(default=None)
    file_path: Mapped[str | None] = mapped_column(String(1000), default=None)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    source: Mapped[str] = mapped_column(String(10), default="sync")

    mapping: Mapped[Mapping] = relationship(back_populates="videos")

    @property
    def display_title(self) -> str:
        return self.custom_title or self.yt_title or self.youtube_id


class SeasonBreak(Base):
    """A new season starts at `video_id`. Season 1 always starts at the first video implicitly."""

    __tablename__ = "season_breaks"

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mappings.id", ondelete="CASCADE"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), unique=True)


class IgnoredVideo(Base):
    __tablename__ = "ignored_videos"
    __table_args__ = (UniqueConstraint("mapping_id", "youtube_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mappings.id", ondelete="CASCADE"), index=True)
    youtube_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(500), default="")
    ignored_at: Mapped[datetime] = mapped_column(default=utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
```

Note for later tasks: always change collections through the relationships (`mapping.videos.append/remove`, `mapping.season_breaks.append/remove`, `mapping.ignored.append/remove`). Sessions use `expire_on_commit=False`, so a row added with only `mapping_id=` would not show up in an already-loaded collection. List order is not refreshed after a position change, so any code that needs order must sort by `position` explicitly.

- [ ] **Step 5: Write `tests/conftest.py`**

```python
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.config import AppConfig
from app.db import init_db, make_engine, make_session_factory
from app.models import Mapping, MappingType, Video


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    media = tmp_path / "media"
    media.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    return AppConfig(media_root=media, config_dir=cfg)


@pytest.fixture
def session_factory(config):
    engine = make_engine(config.db_path)
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def session(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture
def make_mapping(session, config):
    def _make(name: str = "My Show", type: str = MappingType.SHOW, playlist_url: str | None = None, **kw) -> Mapping:
        folder = kw.pop("folder", None) or str(config.media_root / name)
        show_title = kw.pop("show_title", name if type == MappingType.SHOW else None)
        mapping = Mapping(
            name=name, type=type, folder=folder, playlist_url=playlist_url,
            show_title=show_title, overrides={}, **kw,
        )
        session.add(mapping)
        session.commit()
        Path(folder).mkdir(parents=True, exist_ok=True)
        return mapping

    return _make


@pytest.fixture
def add_video(session):
    def _add(mapping: Mapping, youtube_id: str, title: str | None = None, position: int | None = None, **kw) -> Video:
        if position is None:
            position = max((v.position for v in mapping.videos), default=0) + 1
        video = Video(youtube_id=youtube_id, yt_title=title or f"Title {youtube_id}", position=position, **kw)
        mapping.videos.append(video)
        session.commit()
        return video

    return _add


class FakeNotifier:
    """Records every notifier call as (method_name, args)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def record(*args, **kwargs):
            self.calls.append((name, args))

        return record

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


class Clock:
    def __init__(self, start: datetime = datetime(2026, 1, 1, 12, 0, 0)) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()
```

- [ ] **Step 6: Write the failing model tests**

`tests/test_models.py`:
```python
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import IgnoredVideo, Mapping, SeasonBreak, Video, VideoStatus


def test_videos_ordered_by_position(session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "b", position=2)
    add_video(mapping, "a", position=1)
    session.expire_all()
    assert [v.youtube_id for v in session.get(Mapping, mapping.id).videos] == ["a", "b"]


def test_youtube_id_unique_per_mapping(session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "dup")
    with pytest.raises(IntegrityError):
        add_video(mapping, "dup")
    session.rollback()


def test_same_youtube_id_allowed_in_two_mappings(make_mapping, add_video):
    add_video(make_mapping("One"), "same")
    add_video(make_mapping("Two"), "same")


def test_deleting_mapping_cascades(session, make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "x")
    mapping.season_breaks.append(SeasonBreak(video_id=video.id))
    mapping.ignored.append(IgnoredVideo(youtube_id="y", title="Y"))
    session.commit()
    session.delete(mapping)
    session.commit()
    for model in (Video, SeasonBreak, IgnoredVideo):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_video_defaults_and_display_title(make_mapping, add_video):
    video = add_video(make_mapping(), "abc", title="Hello")
    assert video.status == VideoStatus.PENDING
    assert video.attempts == 0
    assert video.user_edited is False
    assert video.display_title == "Hello"
    video.custom_title = "Custom"
    assert video.display_title == "Custom"
```

- [ ] **Step 7: Run tests**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: all 5 PASS. These tests check the schema written in Step 4, so they pass immediately. If any fails, fix the model, not the test.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore app tests
git commit -m "feat: project scaffold, config, database models"
```

---

### Task 2: Settings store with defaults, validation and per-mapping overrides

**Files:**
- Create: `app/settings.py`
- Test: `tests/test_settings.py`

**Interfaces:**
- Consumes: `Setting`, `Mapping` (Task 1)
- Produces:
  - `DEFAULTS: dict[str, Any]`, `CHOICES: dict[str, tuple]`, `OVERRIDABLE: frozenset[str]`, `BOOL_KEYS: frozenset[str]`
  - `class SettingsError(ValueError)`
  - `coerce(key: str, raw: Any) -> Any`
  - `get_all(session) -> dict[str, Any]`
  - `set_many(session, raw: dict[str, Any]) -> dict[str, Any]` (validates, persists, returns merged)
  - `resolve(session, mapping) -> dict[str, Any]` (global merged with the mapping's overrides)
  - `coerce_overrides(raw: dict[str, Any]) -> dict[str, Any]` (drops `""`/`None` = inherit)

- [ ] **Step 1: Write the failing tests**

`tests/test_settings.py`:
```python
import pytest

from app import settings
from app.settings import SettingsError


def test_get_all_returns_defaults(session):
    values = settings.get_all(session)
    assert values["max_height"] == 1080
    assert values["min_delay_seconds"] == 30
    assert values["retry_backoff_minutes"] == [10, 60, 360]


def test_set_many_persists_and_coerces_form_strings(session, session_factory):
    settings.set_many(session, {"max_height": "720", "sleep_requests": "2.5", "ytdlp_auto_update": "off",
                                "retry_backoff_minutes": "5, 30"})
    with session_factory() as other:
        values = settings.get_all(other)
    assert values["max_height"] == 720
    assert values["sleep_requests"] == 2.5
    assert values["ytdlp_auto_update"] is False
    assert values["retry_backoff_minutes"] == [5, 30]


@pytest.mark.parametrize("raw", [
    {"max_height": "999"},
    {"container": "avi"},
    {"min_delay_seconds": "100", "max_delay_seconds": "50"},
    {"concurrency": "0"},
    {"max_filename_length": "20"},
    {"min_delay_seconds": "-1"},
    {"max_height": "abc"},
    {"nope": "1"},
])
def test_set_many_rejects_invalid(session, raw):
    with pytest.raises(SettingsError):
        settings.set_many(session, raw)
    assert settings.get_all(session)["max_height"] == 1080


def test_resolve_applies_only_overridable_non_empty(session, make_mapping):
    mapping = make_mapping()
    mapping.overrides = {"max_height": 480, "container": None, "min_delay_seconds": 1}
    session.commit()
    values = settings.resolve(session, mapping)
    assert values["max_height"] == 480
    assert values["container"] == "mkv"
    assert values["min_delay_seconds"] == 30


def test_coerce_overrides_drops_inherit_values():
    out = settings.coerce_overrides({"max_height": "", "container": "mp4", "subtitle_auto": "false"})
    assert out == {"container": "mp4", "subtitle_auto": False}


def test_coerce_overrides_rejects_non_overridable():
    with pytest.raises(SettingsError):
        settings.coerce_overrides({"concurrency": "2"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_settings.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.settings'`

- [ ] **Step 3: Implement `app/settings.py`**

```python
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Mapping, Setting

DEFAULTS: dict[str, Any] = {
    # quality / format (overridable per mapping)
    "max_height": 1080,
    "container": "mkv",
    "vcodec": "any",
    "subtitles": "off",
    "subtitle_langs": "en",
    "subtitle_auto": False,
    "sponsorblock": "off",
    "sponsorblock_categories": "sponsor,selfpromo,interaction",
    # sync
    "sync_interval_minutes": 360,
    "sync_jitter_seconds": 300,
    # throttling
    "min_delay_seconds": 30,
    "max_delay_seconds": 90,
    "sleep_requests": 1.0,
    "sleep_interval": 0,
    "max_sleep_interval": 0,
    "ratelimit_kbps": 0,
    "concurrency": 1,
    "cooldown_minutes": 60,
    "cooldown_max_minutes": 720,
    # retries
    "retry_backoff_minutes": [10, 60, 360],
    # yt-dlp
    "ytdlp_auto_update": True,
    # naming
    "max_filename_length": 180,
}

CHOICES: dict[str, tuple] = {
    "max_height": (2160, 1440, 1080, 720, 480),
    "container": ("mkv", "mp4"),
    "vcodec": ("any", "h264", "av1", "vp9"),
    "subtitles": ("off", "embed", "sidecar"),
    "sponsorblock": ("off", "mark", "remove"),
}

OVERRIDABLE = frozenset({
    "max_height", "container", "vcodec", "subtitles", "subtitle_langs",
    "subtitle_auto", "sponsorblock", "sponsorblock_categories",
})

BOOL_KEYS = frozenset(k for k, v in DEFAULTS.items() if isinstance(v, bool))


class SettingsError(ValueError):
    pass


def coerce(key: str, raw: Any) -> Any:
    if key not in DEFAULTS:
        raise SettingsError(f"Unknown setting: {key}")
    default = DEFAULTS[key]
    try:
        if isinstance(default, bool):
            value: Any = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "on", "yes")
        elif isinstance(default, int):
            value = int(raw)
        elif isinstance(default, float):
            value = float(raw)
        elif isinstance(default, list):
            parts = raw if isinstance(raw, list) else [p for p in str(raw).replace(" ", "").split(",") if p]
            value = [int(p) for p in parts]
        else:
            value = str(raw).strip()
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"Invalid value for {key}: {raw!r}") from exc
    if key in CHOICES and value not in CHOICES[key]:
        raise SettingsError(f"{key} must be one of {', '.join(map(str, CHOICES[key]))}")
    numbers = value if isinstance(value, list) else [value]
    if any(isinstance(n, (int, float)) and not isinstance(n, bool) and n < 0 for n in numbers):
        raise SettingsError(f"{key} must not be negative")
    return value


def _validate(values: dict[str, Any]) -> None:
    if values["min_delay_seconds"] > values["max_delay_seconds"]:
        raise SettingsError("Minimum delay between downloads must not exceed the maximum delay")
    if values["concurrency"] < 1:
        raise SettingsError("Concurrent downloads must be at least 1")
    if not 60 <= values["max_filename_length"] <= 240:
        raise SettingsError("Max filename length must be between 60 and 240")
    if values["max_sleep_interval"] and values["max_sleep_interval"] < values["sleep_interval"]:
        raise SettingsError("Max sleep interval must be at least the sleep interval")
    if values["cooldown_minutes"] < 1 or values["cooldown_max_minutes"] < values["cooldown_minutes"]:
        raise SettingsError("Cool-down must be at least 1 minute and not exceed the maximum cool-down")
    if values["sync_interval_minutes"] < 5:
        raise SettingsError("Sync interval must be at least 5 minutes")


def get_all(session: Session) -> dict[str, Any]:
    values = copy.deepcopy(DEFAULTS)
    for row in session.scalars(select(Setting)):
        if row.key in values:
            values[row.key] = row.value
    return values


def set_many(session: Session, raw: dict[str, Any]) -> dict[str, Any]:
    new = {key: coerce(key, value) for key, value in raw.items()}
    merged = {**get_all(session), **new}
    _validate(merged)
    for key, value in new.items():
        session.merge(Setting(key=key, value=value))
    session.commit()
    return merged


def resolve(session: Session, mapping: Mapping) -> dict[str, Any]:
    values = get_all(session)
    for key, value in (mapping.overrides or {}).items():
        if key in OVERRIDABLE and value is not None:
            values[key] = value
    return values


def coerce_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in OVERRIDABLE:
            raise SettingsError(f"{key} cannot be overridden per mapping")
        if value is None or value == "":
            continue
        out[key] = coerce(key, value)
    return out
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_settings.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/settings.py tests/test_settings.py
git commit -m "feat: settings store with validation and per-mapping overrides"
```

---

### Task 3: Naming rules (sanitize, season/episode numbering, file stems)

**Files:**
- Create: `app/naming.py`
- Test: `tests/test_naming.py`

**Interfaces:**
- Produces:
  - `sanitize(name: str, max_len: int = 180, fallback: str = "untitled") -> str`
  - `@dataclass(frozen=True) NamingItem(video_id: int, title: str = "", artist: str = "", song: str = "", season_break: bool = False)`
  - `season_episode(items: list[NamingItem]) -> dict[int, tuple[int, int]]`
  - `episode_code(season: int, episode: int) -> str` → `"S01E02"`
  - `compute_stems(mapping_type: str, show_title: str, items: list[NamingItem], max_len: int = 180) -> dict[int, str]`. Returns a relative path without extension: `"Season 01/Show S01E01 Title"` for shows, `"Artist - Song"` for music videos.

- [ ] **Step 1: Write the failing tests**

`tests/test_naming.py`:
```python
import pytest

from app.naming import NamingItem, compute_stems, episode_code, sanitize, season_episode


@pytest.mark.parametrize("raw, expected", [
    ('AC/DC: "Live" <2024>?', "AC DC Live 2024"),
    ("  lots   of\tspace  ", "lots of space"),
    ("trailing dots...", "trailing dots"),
    (".hidden", "hidden"),
    ("", "untitled"),
    ("???", "untitled"),
    ("日本語のタイトル 🎵", "日本語のタイトル 🎵"),
])
def test_sanitize(raw, expected):
    assert sanitize(raw) == expected


def test_sanitize_truncates_and_strips_after_cut():
    assert sanitize("a" * 10 + " . b", max_len=12) == "a" * 10


def test_sanitize_custom_fallback():
    assert sanitize("///", fallback="") == ""


def test_season_episode_with_breaks():
    items = [NamingItem(1), NamingItem(2), NamingItem(3, season_break=True), NamingItem(4)]
    assert season_episode(items) == {1: (1, 1), 2: (1, 2), 3: (2, 1), 4: (2, 2)}


def test_break_on_first_item_is_ignored():
    items = [NamingItem(1, season_break=True), NamingItem(2)]
    assert season_episode(items) == {1: (1, 1), 2: (1, 2)}


def test_episode_code():
    assert episode_code(1, 2) == "S01E02"
    assert episode_code(12, 105) == "S12E105"


def test_show_stems():
    items = [NamingItem(1, title="Intro"), NamingItem(2, title="Part: Two", season_break=True)]
    assert compute_stems("show", "My Show", items) == {
        1: "Season 01/My Show S01E01 Intro",
        2: "Season 02/My Show S02E01 Part Two",
    }


def test_music_video_stems_with_duplicates():
    items = [
        NamingItem(1, artist="Queen", song="Bohemian Rhapsody"),
        NamingItem(2, artist="queen", song="bohemian rhapsody"),
        NamingItem(3, artist="Queen", song="Bohemian Rhapsody (2)"),
        NamingItem(4, artist="", song=""),
    ]
    stems = compute_stems("music_video", "", items)
    assert stems[1] == "Queen - Bohemian Rhapsody"
    assert stems[2] == "queen - bohemian rhapsody (2)"
    assert stems[3] == "Queen - Bohemian Rhapsody (2) (2)"
    assert stems[4] == "Unknown Artist - Untitled"


def test_hostile_titles():
    items = [
        NamingItem(1, title="???"),
        NamingItem(2, title="🔥" * 5 + " 日本 "),
        NamingItem(3, title="x" * 300),
    ]
    stems = compute_stems("show", "Show", items, max_len=100)
    assert stems[1] == "Season 01/Show S01E01"
    assert stems[2] == "Season 01/Show S01E02 🔥🔥🔥🔥🔥 日本"
    name = stems[3].split("/")[1]
    assert name.startswith("Show S01E03 x")
    assert len(name) <= 100
    assert len(set(stems.values())) == 3


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        compute_stems("movie", "", [NamingItem(1)])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_naming.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.naming'`

- [ ] **Step 3: Implement `app/naming.py`**

```python
"""Pure naming rules. No DB, no filesystem."""
from __future__ import annotations

import re
from dataclasses import dataclass

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_SPACES = re.compile(r"\s+")


def sanitize(name: str, max_len: int = 180, fallback: str = "untitled") -> str:
    cleaned = _INVALID.sub(" ", name)
    cleaned = _SPACES.sub(" ", cleaned).strip().lstrip(".").strip()
    cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or fallback


@dataclass(frozen=True)
class NamingItem:
    video_id: int
    title: str = ""
    artist: str = ""
    song: str = ""
    season_break: bool = False


def season_episode(items: list[NamingItem]) -> dict[int, tuple[int, int]]:
    result: dict[int, tuple[int, int]] = {}
    season, episode = 1, 0
    for index, item in enumerate(items):
        if item.season_break and index > 0:
            season += 1
            episode = 0
        episode += 1
        result[item.video_id] = (season, episode)
    return result


def episode_code(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def _show_stem(show_title: str, season: int, episode: int, title: str, max_len: int) -> str:
    prefix = f"{sanitize(show_title, max_len // 2, fallback='Show')} {episode_code(season, episode)}"
    title_part = sanitize(title, max(max_len - len(prefix) - 1, 0), fallback="")
    name = f"{prefix} {title_part}" if title_part else prefix
    return f"Season {season:02d}/{name}"


def _music_stem(artist: str, song: str, max_len: int) -> str:
    artist_part = sanitize(artist, max_len // 2, fallback="Unknown Artist")
    song_part = sanitize(song, max_len - len(artist_part) - 3, fallback="Untitled")
    return f"{artist_part} - {song_part}"


def compute_stems(mapping_type: str, show_title: str, items: list[NamingItem], max_len: int = 180) -> dict[int, str]:
    if mapping_type == "show":
        numbers = season_episode(items)
        return {i.video_id: _show_stem(show_title, *numbers[i.video_id], i.title, max_len) for i in items}
    if mapping_type == "music_video":
        stems: dict[int, str] = {}
        used: set[str] = set()
        for item in items:
            base = _music_stem(item.artist, item.song, max_len - 5)
            candidate, n = base, 1
            while candidate.casefold() in used:
                n += 1
                candidate = f"{base} ({n})"
            used.add(candidate.casefold())
            stems[item.video_id] = candidate
        return stems
    raise ValueError(f"Unknown mapping type: {mapping_type}")
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_naming.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/naming.py tests/test_naming.py
git commit -m "feat: Jellyfin naming rules for shows and music videos"
```

---

### Task 4: Music-video artist/song parsing

**Files:**
- Create: `app/mvparse.py`
- Test: `tests/test_mvparse.py`

**Interfaces:**
- Produces: `parse_artist_song(title: str, channel: str | None = None, yt_artist: str | None = None, yt_track: str | None = None) -> tuple[str, str]`, `strip_noise(text: str) -> str`, `clean_channel(channel: str | None) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_mvparse.py`:
```python
import pytest

from app.mvparse import clean_channel, parse_artist_song, strip_noise

CASES = [
    # (title, channel, yt_artist, yt_track, expected)
    ("Rick Astley - Never Gonna Give You Up (Official Music Video)", "Rick Astley", None, None,
     ("Rick Astley", "Never Gonna Give You Up")),
    ("Daft Punk - Get Lucky (Official Audio) ft. Pharrell Williams, Nile Rodgers", "Daft Punk", None, None,
     ("Daft Punk", "Get Lucky ft. Pharrell Williams, Nile Rodgers")),
    ("Billie Eilish – bad guy [4K]", "Billie Eilish", None, None, ("Billie Eilish", "bad guy")),
    ("Eminem - Lose Yourself (feat. Someone) [Official Video]", "EminemVEVO", None, None,
     ("Eminem", "Lose Yourself (feat. Someone)")),
    ('Artist - "Quoted Song" (Official Video)', "x", None, None, ("Artist", "Quoted Song")),
    ("Coldplay - Yellow | Official Video", "Coldplay", None, None, ("Coldplay", "Yellow")),
    ("Kanye West - Stronger (HD)", "x", None, None, ("Kanye West", "Stronger")),
    ("Hello", "AdeleVEVO", None, None, ("Adele", "Hello")),
    ("Song Name (Lyrics)", "Some Artist - Topic", None, None, ("Some Artist", "Song Name")),
    ("whatever title", "x", "Queen", "Bohemian Rhapsody (Remastered 2011)", ("Queen", "Bohemian Rhapsody")),
    ("Mystery Track", None, None, None, ("Unknown Artist", "Mystery Track")),
    ("Artist - Song (Live at Wembley)", "x", None, None, ("Artist", "Song (Live at Wembley)")),
]


@pytest.mark.parametrize("title, channel, yt_artist, yt_track, expected", CASES)
def test_parse_artist_song(title, channel, yt_artist, yt_track, expected):
    assert parse_artist_song(title, channel, yt_artist, yt_track) == expected


def test_strip_noise_keeps_feat():
    assert strip_noise("Song (feat. X) (Visualizer)") == "Song (feat. X)"


@pytest.mark.parametrize("channel, expected", [
    ("TaylorSwiftVEVO", "TaylorSwift"),
    ("Some Artist - Topic", "Some Artist"),
    ("Band Official", "Band"),
    ("Band Official Channel", "Band"),
    (None, ""),
])
def test_clean_channel(channel, expected):
    assert clean_channel(channel) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mvparse.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.mvparse'`

- [ ] **Step 3: Implement `app/mvparse.py`**

```python
"""Best-effort artist/song extraction from messy YouTube titles."""
from __future__ import annotations

import re

_NOISE_WORDS = (
    r"official|video|audio|lyrics?|visuali[sz]er|hd|hq|4k|8k|1080p|720p|"
    r"remaster(?:ed)?|mv|m/v|clip officiel|videoclip|explicit|color coded"
)
_BRACKETED_NOISE = re.compile(
    r"\s*[\(\[【][^\)\]】]*?\b(?:" + _NOISE_WORDS + r")\b[^\)\]】]*?[\)\]】]",
    re.IGNORECASE,
)
_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_CHANNEL_SUFFIX = re.compile(
    r"(?:\s*-\s*topic|vevo|\s+official(?:\s+(?:artist\s+)?channel)?)\s*$", re.IGNORECASE
)
_QUOTES = "\"'“”‘’"


def strip_noise(text: str) -> str:
    cleaned = _BRACKETED_NOISE.sub("", text)
    if " | " in cleaned:
        cleaned = cleaned.split(" | ", 1)[0]
    return cleaned.strip().strip(_QUOTES).strip()


def clean_channel(channel: str | None) -> str:
    if not channel:
        return ""
    name, previous = channel.strip(), None
    while previous != name:
        previous = name
        name = _CHANNEL_SUFFIX.sub("", name).strip()
    return name


def parse_artist_song(
    title: str,
    channel: str | None = None,
    yt_artist: str | None = None,
    yt_track: str | None = None,
) -> tuple[str, str]:
    if yt_artist and yt_track:
        return yt_artist.strip(), strip_noise(yt_track)
    parts = _SEPARATOR.split(title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return strip_noise(parts[0]), strip_noise(parts[1])
    return clean_channel(channel) or "Unknown Artist", strip_noise(title)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_mvparse.py -v`
Expected: all PASS. If a real-world case fails, adjust the regexes, never the expected values.

- [ ] **Step 5: Commit**

```bash
git add app/mvparse.py tests/test_mvparse.py
git commit -m "feat: music video artist/song parsing"
```

---

### Task 5: NFO generation

**Files:**
- Create: `app/nfo.py`
- Test: `tests/test_nfo.py`

**Interfaces:**
- Produces: `format_date(yyyymmdd: str | None) -> str | None`, `tvshow_nfo(title: str) -> str`, `episode_nfo(*, title, show_title, season, episode, plot, aired, youtube_id) -> str`, `musicvideo_nfo(*, title, artist, plot, premiered, youtube_id) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_nfo.py`:
```python
from xml.etree import ElementTree as ET

from app.nfo import episode_nfo, format_date, musicvideo_nfo, tvshow_nfo


def test_format_date():
    assert format_date("20240131") == "2024-01-31"
    assert format_date(None) is None
    assert format_date("2024") is None


def test_episode_nfo_fields():
    xml = episode_nfo(title="A & B", show_title="Show", season=2, episode=3, plot="Line1\nLine2\x07",
                      aired="20240131", youtube_id="abc")
    root = ET.fromstring(xml.split("\n", 1)[1])
    assert root.tag == "episodedetails"
    assert root.findtext("title") == "A & B"
    assert root.findtext("showtitle") == "Show"
    assert root.findtext("season") == "2"
    assert root.findtext("episode") == "3"
    assert root.findtext("plot") == "Line1\nLine2"
    assert root.findtext("aired") == "2024-01-31"
    uid = root.find("uniqueid")
    assert uid.get("type") == "youtube" and uid.text == "abc"


def test_missing_values_are_omitted():
    xml = episode_nfo(title="T", show_title="S", season=1, episode=1, plot=None, aired=None, youtube_id="x")
    root = ET.fromstring(xml.split("\n", 1)[1])
    assert root.find("plot") is None
    assert root.find("aired") is None


def test_musicvideo_nfo():
    xml = musicvideo_nfo(title="Song", artist="Artist", plot="p", premiered="20200101", youtube_id="y")
    root = ET.fromstring(xml.split("\n", 1)[1])
    assert root.tag == "musicvideo"
    assert root.findtext("artist") == "Artist"
    assert root.findtext("premiered") == "2020-01-01"
    assert root.findtext("year") == "2020"


def test_tvshow_nfo():
    assert "<title>My Show</title>" in tvshow_nfo("My Show")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_nfo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.nfo'`

- [ ] **Step 3: Implement `app/nfo.py`**

```python
"""Kodi-style NFO files, which Jellyfin's NFO metadata reader understands."""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_HEADER = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'


def format_date(yyyymmdd: str | None) -> str | None:
    if not yyyymmdd or len(yyyymmdd) != 8 or not yyyymmdd.isdigit():
        return None
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _add(parent: ET.Element, tag: str, value: object) -> None:
    if value is None or value == "":
        return
    ET.SubElement(parent, tag).text = _CONTROL.sub("", str(value))


def _uniqueid(parent: ET.Element, youtube_id: str) -> None:
    uid = ET.SubElement(parent, "uniqueid", type="youtube", default="true")
    uid.text = youtube_id


def _render(root: ET.Element) -> str:
    ET.indent(root)
    return _HEADER + ET.tostring(root, encoding="unicode") + "\n"


def tvshow_nfo(title: str) -> str:
    root = ET.Element("tvshow")
    _add(root, "title", title)
    return _render(root)


def episode_nfo(*, title: str, show_title: str, season: int, episode: int, plot: str | None,
                aired: str | None, youtube_id: str) -> str:
    root = ET.Element("episodedetails")
    _add(root, "title", title)
    _add(root, "showtitle", show_title)
    _add(root, "season", season)
    _add(root, "episode", episode)
    _add(root, "plot", plot)
    _add(root, "aired", format_date(aired))
    _uniqueid(root, youtube_id)
    return _render(root)


def musicvideo_nfo(*, title: str, artist: str, plot: str | None, premiered: str | None, youtube_id: str) -> str:
    root = ET.Element("musicvideo")
    date = format_date(premiered)
    _add(root, "title", title)
    _add(root, "artist", artist)
    _add(root, "plot", plot)
    _add(root, "premiered", date)
    _add(root, "year", date[:4] if date else None)
    _uniqueid(root, youtube_id)
    return _render(root)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_nfo.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/nfo.py tests/test_nfo.py
git commit -m "feat: NFO generation for episodes, shows and music videos"
```

---
### Task 6: Organizer core: make the disk match the DB

**Files:**
- Create: `app/organizer.py`
- Test: `tests/test_organizer.py`

**Interfaces:**
- Consumes: `naming.NamingItem`, `naming.compute_stems`, `naming.season_episode` (Task 3); `nfo.*` (Task 5); models (Task 1)
- Produces:
  - `class PathError(ValueError)`
  - `ensure_under_root(media_root: Path, path: Path | str) -> Path` (resolved; must be strictly inside the root and not inside `.ytjs-tmp`)
  - `mapping_dir(media_root: Path, mapping: Mapping) -> Path`
  - `ordered_videos(mapping: Mapping) -> list[Video]` (sorted by position)
  - `naming_items(mapping: Mapping) -> list[NamingItem]` (skips `unavailable` videos and moves their season break onto the next video)
  - `target_stems(mapping: Mapping, max_len: int) -> dict[int, str]`
  - `companion_files(media_path: Path) -> list[Path]`
  - `execute_moves(moves: list[tuple[Path, Path]]) -> None`
  - `apply(session: Session, media_root: Path, mapping: Mapping, max_len: int) -> None`
  - `THUMB_EXTS: tuple[str, ...]`

- [ ] **Step 1: Write the failing tests**

`tests/test_organizer.py`:
```python
from pathlib import Path

import pytest

from app import organizer
from app.models import MappingType, SeasonBreak, VideoStatus
from app.organizer import PathError


def put(folder: Path, rel: str, content: str) -> Path:
    path = folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def done_video(add_video):
    def _done(mapping, yid, rel, title=None, **kw):
        folder = Path(mapping.folder)
        put(folder, rel, f"media {yid}")
        put(folder, str(Path(rel).with_suffix("")) + "-thumb.jpg", f"thumb {yid}")
        return add_video(mapping, yid, title=title, status=VideoStatus.DONE, file_path=rel, **kw)
    return _done


def apply(session, config, mapping):
    organizer.apply(session, config.media_root, mapping, 180)


def test_ensure_under_root(config):
    root = config.media_root
    assert organizer.ensure_under_root(root, root / "a" / "b") == (root / "a" / "b").resolve()
    for bad in [root, root / ".." / "x", Path("/etc"), root / ".ytjs-tmp" / "x"]:
        with pytest.raises(PathError):
            organizer.ensure_under_root(root, bad)


def test_companion_files(tmp_path):
    media = put(tmp_path, "Show S01E01 A.mkv", "m")
    expected = {put(tmp_path, n, "c") for n in ["Show S01E01 A.nfo", "Show S01E01 A-thumb.jpg", "Show S01E01 A.en.srt"]}
    put(tmp_path, "Show S01E01 A B.mkv", "other")
    put(tmp_path, "Show S01E01 A B.nfo", "other")
    assert set(organizer.companion_files(media)) == expected


def test_apply_places_files_with_companions_and_nfo(session, config, make_mapping, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "random/a.mkv", title="First")
    apply(session, config, mapping)
    folder = Path(mapping.folder)
    media = folder / "Season 01" / "My Show S01E01 First.mkv"
    assert media.read_text() == "media a"
    assert (folder / "Season 01" / "My Show S01E01 First-thumb.jpg").read_text() == "thumb a"
    nfo = (folder / "Season 01" / "My Show S01E01 First.nfo").read_text()
    assert "<episode>1</episode>" in nfo and "<title>First</title>" in nfo
    assert "<title>My Show</title>" in (folder / "tvshow.nfo").read_text()
    assert mapping.videos[0].file_path == "Season 01/My Show S01E01 First.mkv"


def test_apply_swaps_two_episodes_without_collision(session, config, make_mapping, done_video):
    mapping = make_mapping()
    a = done_video(mapping, "a", "x/a.mkv", title="A")
    b = done_video(mapping, "b", "x/b.mkv", title="B")
    apply(session, config, mapping)
    a.position, b.position = 2, 1
    session.commit()
    apply(session, config, mapping)
    season = Path(mapping.folder) / "Season 01"
    assert (season / "My Show S01E01 B.mkv").read_text() == "media b"
    assert (season / "My Show S01E02 A.mkv").read_text() == "media a"
    assert (season / "My Show S01E02 A-thumb.jpg").read_text() == "thumb a"
    assert "<episode>2</episode>" in (season / "My Show S01E02 A.nfo").read_text()
    assert sorted(p.name for p in season.iterdir()) == sorted([
        "My Show S01E01 B.mkv", "My Show S01E01 B-thumb.jpg", "My Show S01E01 B.nfo",
        "My Show S01E02 A.mkv", "My Show S01E02 A-thumb.jpg", "My Show S01E02 A.nfo",
    ])


def test_season_break_moves_files_and_cleans_empty_dirs(session, config, make_mapping, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "x/a.mkv", title="A")
    b = done_video(mapping, "b", "x/b.mkv", title="B")
    apply(session, config, mapping)
    mapping.season_breaks.append(SeasonBreak(video_id=b.id))
    session.commit()
    apply(session, config, mapping)
    folder = Path(mapping.folder)
    assert (folder / "Season 02" / "My Show S02E01 B.mkv").exists()
    mapping.season_breaks.clear()
    session.commit()
    apply(session, config, mapping)
    assert (folder / "Season 01" / "My Show S01E02 B.mkv").exists()
    assert not (folder / "Season 02").exists()
    assert not (folder / "x").exists() or not any((folder / "x").iterdir())


def test_unavailable_videos_do_not_consume_numbers_and_pass_on_breaks(session, config, make_mapping, add_video, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "x/a.mkv", title="A")
    gone = add_video(mapping, "gone", status=VideoStatus.UNAVAILABLE)
    done_video(mapping, "c", "x/c.mkv", title="C")
    mapping.season_breaks.append(SeasonBreak(video_id=gone.id))
    session.commit()
    apply(session, config, mapping)
    assert (Path(mapping.folder) / "Season 02" / "My Show S02E01 C.mkv").exists()


def test_pending_videos_reserve_episode_numbers(session, config, make_mapping, add_video, done_video):
    mapping = make_mapping()
    add_video(mapping, "p")
    done_video(mapping, "d", "x/d.mkv", title="D")
    apply(session, config, mapping)
    assert (Path(mapping.folder) / "Season 01" / "My Show S01E02 D.mkv").exists()


def test_custom_title_renames(session, config, make_mapping, done_video):
    mapping = make_mapping()
    video = done_video(mapping, "a", "x/a.mkv", title="Old")
    apply(session, config, mapping)
    video.custom_title = "New"
    session.commit()
    apply(session, config, mapping)
    assert (Path(mapping.folder) / "Season 01" / "My Show S01E01 New.mkv").read_text() == "media a"
    assert "<title>New</title>" in (Path(mapping.folder) / "Season 01" / "My Show S01E01 New.nfo").read_text()


def test_music_video_layout(session, config, make_mapping, done_video):
    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    done_video(mapping, "a", "a.mkv", artist="Queen", song="Bohemian Rhapsody")
    done_video(mapping, "b", "b.mkv", artist="Queen", song="Bohemian Rhapsody")
    apply(session, config, mapping)
    folder = Path(mapping.folder)
    assert (folder / "Queen - Bohemian Rhapsody.mkv").read_text() == "media a"
    assert (folder / "Queen - Bohemian Rhapsody (2).mkv").read_text() == "media b"
    nfo = (folder / "Queen - Bohemian Rhapsody.nfo").read_text()
    assert "<artist>Queen</artist>" in nfo and "<title>Bohemian Rhapsody</title>" in nfo
    assert not (folder / "tvshow.nfo").exists()


def test_apply_never_overwrites_foreign_file(session, config, make_mapping, done_video):
    mapping = make_mapping()
    a = done_video(mapping, "a", "x/a.mkv", title="A")
    b = done_video(mapping, "b", "x/b.mkv", title="B")
    folder = Path(mapping.folder)
    foreign = put(folder, "Season 01/My Show S01E01 A.mkv", "FOREIGN")
    apply(session, config, mapping)
    assert foreign.read_text() == "FOREIGN"
    assert (folder / "x" / "a.mkv").read_text() == "media a"
    assert a.file_path == "x/a.mkv"
    assert (folder / "Season 01" / "My Show S01E02 B.mkv").read_text() == "media b"
    assert b.file_path == "Season 01/My Show S01E02 B.mkv"


def test_apply_is_idempotent(session, config, make_mapping, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "x/a.mkv", title="A")
    apply(session, config, mapping)
    nfo = Path(mapping.folder) / "Season 01" / "My Show S01E01 A.nfo"
    mtime = nfo.stat().st_mtime_ns
    apply(session, config, mapping)
    assert nfo.stat().st_mtime_ns == mtime


def test_poster_copied_from_first_episode_thumb(session, config, make_mapping, done_video):
    mapping = make_mapping()
    done_video(mapping, "a", "x/a.mkv", title="A")
    apply(session, config, mapping)
    assert (Path(mapping.folder) / "poster.jpg").read_text() == "thumb a"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_organizer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.organizer'`

- [ ] **Step 3: Implement `app/organizer.py`**

```python
"""Makes the filesystem match the database. The DB is the source of truth."""
from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import naming, nfo
from app.models import Mapping, MappingType, Video, VideoStatus

log = logging.getLogger(__name__)

THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")
STAGING_NAME = ".ytjs-tmp"
_COMPANION_TAIL = re.compile(r"\.nfo|-thumb\.(?:jpg|jpeg|png|webp)|\.[A-Za-z0-9_-]{1,15}\.(?:srt|vtt|ass)")
_SEASON_DIR = re.compile(r"Season \d+")


class PathError(ValueError):
    pass


def ensure_under_root(media_root: Path, path: Path | str) -> Path:
    root = media_root.resolve()
    resolved = Path(path).resolve()
    if resolved == root or root not in resolved.parents:
        raise PathError(f"{path} must be a folder inside {media_root}")
    staging = root / STAGING_NAME
    if resolved == staging or staging in resolved.parents:
        raise PathError(f"{path} is inside the internal staging folder")
    return resolved


def mapping_dir(media_root: Path, mapping: Mapping) -> Path:
    return ensure_under_root(media_root, mapping.folder)


def ordered_videos(mapping: Mapping) -> list[Video]:
    return sorted(mapping.videos, key=lambda v: v.position)


def naming_items(mapping: Mapping) -> list[naming.NamingItem]:
    break_ids = {b.video_id for b in mapping.season_breaks}
    items: list[naming.NamingItem] = []
    carry = False
    for video in ordered_videos(mapping):
        is_break = video.id in break_ids or carry
        if video.status == VideoStatus.UNAVAILABLE:
            carry = is_break
            continue
        carry = False
        items.append(naming.NamingItem(
            video_id=video.id, title=video.display_title,
            artist=video.artist or "", song=video.song or "", season_break=is_break,
        ))
    return items


def target_stems(mapping: Mapping, max_len: int) -> dict[int, str]:
    return naming.compute_stems(mapping.type, mapping.show_title or mapping.name, naming_items(mapping), max_len)


def companion_files(media_path: Path) -> list[Path]:
    stem = media_path.stem
    if not media_path.parent.is_dir():
        return []
    return [
        p for p in media_path.parent.iterdir()
        if p != media_path and p.is_file() and p.name.startswith(stem)
        and _COMPANION_TAIL.fullmatch(p.name[len(stem):])
    ]


def execute_moves(moves: list[tuple[Path, Path]]) -> None:
    """Rename src->dst in two phases (via unique temp names) so swaps and cycles never collide."""
    staged: list[tuple[Path, Path]] = []
    for src, dst in moves:
        tmp = src.with_name(f".ytjs-move-{uuid.uuid4().hex}")
        os.rename(src, tmp)
        staged.append((tmp, dst))
    for tmp, dst in staged:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            raise FileExistsError(f"Refusing to overwrite {dst}")
        os.rename(tmp, dst)


def _group_moves(folder: Path, current: Path, target_rel: str) -> list[tuple[Path, Path]]:
    target = folder / target_rel
    new_stem = target.stem
    moves = [(current, target)]
    for companion in companion_files(current):
        tail = companion.name[len(current.stem):]
        moves.append((companion, target.with_name(new_stem + tail)))
    return moves


def apply(session: Session, media_root: Path, mapping: Mapping, max_len: int) -> None:
    folder = mapping_dir(media_root, mapping)
    folder.mkdir(parents=True, exist_ok=True)
    stems = target_stems(mapping, max_len)

    plans: list[tuple[Video, str, list[tuple[Path, Path]]]] = []
    for video in ordered_videos(mapping):
        if video.status != VideoStatus.DONE or not video.file_path or video.id not in stems:
            continue
        current = folder / video.file_path
        target_rel = stems[video.id] + current.suffix
        if video.file_path == target_rel:
            continue
        if not current.exists():
            log.warning("File for video %s missing: %s", video.youtube_id, current)
            continue
        plans.append((video, target_rel, _group_moves(folder, current, target_rel)))

    # Drop plans whose targets are occupied by files that are not themselves moving away.
    # Repeat until stable, because dropping one plan keeps its source file in place.
    accepted = plans
    while True:
        moving = {src for _, _, group in accepted for src, _ in group}
        kept = [p for p in accepted if not any(dst.exists() and dst not in moving for _, dst in p[2])]
        if len(kept) == len(accepted):
            break
        accepted = kept
    for video, target_rel, _ in plans:
        if all(video is not v for v, _, _ in accepted):
            log.error("Not renaming %s to %s: target already exists", video.file_path, target_rel)

    if accepted:
        execute_moves([move for _, _, group in accepted for move in group])
        for video, target_rel, _ in accepted:
            video.file_path = target_rel
        session.commit()

    _write_metadata(folder, mapping)
    _remove_empty_dirs(folder)


def _write_if_changed(path: Path, content: str) -> None:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def _write_metadata(folder: Path, mapping: Mapping) -> None:
    show_title = mapping.show_title or mapping.name
    is_show = mapping.type == MappingType.SHOW
    numbers = naming.season_episode(naming_items(mapping)) if is_show else {}
    if is_show:
        _write_if_changed(folder / "tvshow.nfo", nfo.tvshow_nfo(show_title))
    for video in ordered_videos(mapping):
        if video.status != VideoStatus.DONE or not video.file_path:
            continue
        media = folder / video.file_path
        if not media.exists():
            continue
        if is_show:
            season, episode = numbers[video.id]
            content = nfo.episode_nfo(
                title=video.display_title, show_title=show_title, season=season, episode=episode,
                plot=video.description, aired=video.upload_date, youtube_id=video.youtube_id,
            )
        else:
            content = nfo.musicvideo_nfo(
                title=video.song or video.display_title, artist=video.artist or "",
                plot=video.description, premiered=video.upload_date, youtube_id=video.youtube_id,
            )
        _write_if_changed(media.with_suffix(".nfo"), content)
    if is_show:
        _ensure_poster(folder, mapping)


def _ensure_poster(folder: Path, mapping: Mapping) -> None:
    if any((folder / f"poster{ext}").exists() for ext in THUMB_EXTS):
        return
    for video in ordered_videos(mapping):
        if video.status != VideoStatus.DONE or not video.file_path:
            continue
        media = folder / video.file_path
        thumb = media.with_name(media.stem + "-thumb.jpg")
        if thumb.exists():
            shutil.copyfile(thumb, folder / "poster.jpg")
            return


def _remove_empty_dirs(folder: Path) -> None:
    for path in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(path.iterdir()):
            path.rmdir()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_organizer.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/organizer.py tests/test_organizer.py
git commit -m "feat: organizer applies DB naming to disk with collision-safe renames"
```

---

### Task 7: Organizer: place downloads, delete files, startup reconcile

**Files:**
- Modify: `app/organizer.py` (append functions)
- Test: `tests/test_organizer_place.py`

**Interfaces:**
- Consumes: `apply`, `target_stems`, `companion_files`, `mapping_dir`, `PathError`, `THUMB_EXTS` (Task 6)
- Produces:
  - `place(session, media_root: Path, mapping: Mapping, video: Video, staged_media: Path, staged_extras: list[Path], max_len: int) -> None`. Moves `media.<ext>` → target, `media.jpg` → `<stem>-thumb.jpg`, `media.en.srt` → `<stem>.en.srt`; sets `status=done` and `file_path`; then runs `apply`.
  - `delete_video_files(media_root: Path, mapping: Mapping, video: Video) -> None`
  - `delete_mapping_folder(media_root: Path, mapping_folder: str) -> None`
  - `reconcile(session, media_root: Path, staging_dir: Path, max_len: int) -> None`

- [ ] **Step 1: Write the failing tests**

`tests/test_organizer_place.py`:
```python
from pathlib import Path

from app import organizer
from app.models import Video, VideoStatus


def staged(tmp_path: Path, names: dict[str, str]) -> list[Path]:
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    paths = []
    for name, content in names.items():
        p = stage / name
        p.write_text(content)
        paths.append(p)
    return paths


def test_place_moves_media_thumb_and_subs(session, config, make_mapping, add_video, tmp_path):
    mapping = make_mapping()
    video = add_video(mapping, "a", title="Hello", status=VideoStatus.DOWNLOADING)
    media, thumb, subs = staged(tmp_path, {"media.mkv": "M", "media.jpg": "T", "media.en.srt": "S"})
    organizer.place(session, config.media_root, mapping, video, media, [thumb, subs], 180)
    season = Path(mapping.folder) / "Season 01"
    assert (season / "My Show S01E01 Hello.mkv").read_text() == "M"
    assert (season / "My Show S01E01 Hello-thumb.jpg").read_text() == "T"
    assert (season / "My Show S01E01 Hello.en.srt").read_text() == "S"
    assert (season / "My Show S01E01 Hello.nfo").exists()
    assert video.status == VideoStatus.DONE
    assert video.file_path == "Season 01/My Show S01E01 Hello.mkv"


def test_place_replaces_previous_download(session, config, make_mapping, add_video, tmp_path):
    mapping = make_mapping()
    folder = Path(mapping.folder)
    old = folder / "old.mp4"
    old.write_text("OLD")
    (folder / "old-thumb.jpg").write_text("OLDTHUMB")
    video = add_video(mapping, "a", title="Hello", status=VideoStatus.DOWNLOADING, file_path="old.mp4")
    [media] = staged(tmp_path, {"media.mkv": "NEW"})
    organizer.place(session, config.media_root, mapping, video, media, [], 180)
    assert not old.exists() and not (folder / "old-thumb.jpg").exists()
    assert (folder / "Season 01" / "My Show S01E01 Hello.mkv").read_text() == "NEW"


def test_delete_video_files(session, config, make_mapping, add_video, tmp_path):
    mapping = make_mapping()
    video = add_video(mapping, "a", title="Hello", status=VideoStatus.DOWNLOADING)
    media, thumb = staged(tmp_path, {"media.mkv": "M", "media.jpg": "T"})
    organizer.place(session, config.media_root, mapping, video, media, [thumb], 180)
    organizer.delete_video_files(config.media_root, mapping, video)
    season = Path(mapping.folder) / "Season 01"
    assert not season.exists() or list(season.iterdir()) == []


def test_delete_mapping_folder_refuses_outside_root(config, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        organizer.delete_mapping_folder(config.media_root, str(outside))
    except organizer.PathError:
        pass
    assert outside.exists()


def test_reconcile(session, config, make_mapping, add_video):
    mapping = make_mapping()
    downloading = add_video(mapping, "d", status=VideoStatus.DOWNLOADING)
    missing = add_video(mapping, "m", status=VideoStatus.DONE, file_path="gone.mkv")
    leftover = config.staging_dir / "5" / "media.mkv.part"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("x")
    organizer.reconcile(session, config.media_root, config.staging_dir, 180)
    session.expire_all()
    assert session.get(Video, downloading.id).status == VideoStatus.PENDING
    reloaded = session.get(Video, missing.id)
    assert reloaded.status == VideoStatus.PENDING and reloaded.file_path is None
    assert config.staging_dir.exists() and list(config.staging_dir.iterdir()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_organizer_place.py -v`
Expected: FAIL with `AttributeError: module 'app.organizer' has no attribute 'place'`

- [ ] **Step 3: Append to `app/organizer.py`**

Add `from sqlalchemy import select` to the imports at the top of `app/organizer.py`. Then append:

```python
def place(session: Session, media_root: Path, mapping: Mapping, video: Video,
          staged_media: Path, staged_extras: list[Path], max_len: int) -> None:
    """Move a finished download from staging into the library under its target name."""
    folder = mapping_dir(media_root, mapping)
    if video.file_path:
        old = folder / video.file_path
        for path in [*companion_files(old), old]:
            path.unlink(missing_ok=True)
    video.status = VideoStatus.DONE
    stem = target_stems(mapping, max_len)[video.id]
    target = folder / f"{stem}{staged_media.suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(staged_media, target)
    for extra in staged_extras:
        tail = extra.name[len(staged_media.stem):]
        if extra.suffix.lower() in THUMB_EXTS:
            tail = "-thumb" + extra.suffix.lower()
        shutil.move(extra, target.with_name(target.stem + tail))
    video.file_path = target.relative_to(folder).as_posix()
    session.commit()
    apply(session, media_root, mapping, max_len)


def delete_video_files(media_root: Path, mapping: Mapping, video: Video) -> None:
    if not video.file_path:
        return
    media = mapping_dir(media_root, mapping) / video.file_path
    for path in [*companion_files(media), media]:
        path.unlink(missing_ok=True)


def delete_mapping_folder(media_root: Path, mapping_folder: str) -> None:
    folder = ensure_under_root(media_root, mapping_folder)
    if folder.exists():
        shutil.rmtree(folder)


def reconcile(session: Session, media_root: Path, staging_dir: Path, max_len: int) -> None:
    """Startup repair: clean staging, requeue interrupted/missing downloads, re-apply naming."""
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    for video in session.scalars(select(Video).where(Video.status == VideoStatus.DOWNLOADING)).all():
        video.status = VideoStatus.PENDING
    session.commit()
    for mapping in session.scalars(select(Mapping)).all():
        try:
            folder = mapping_dir(media_root, mapping)
        except PathError:
            log.error("Mapping %s has an invalid folder %s", mapping.name, mapping.folder)
            continue
        for video in mapping.videos:
            if video.status == VideoStatus.DONE and (not video.file_path or not (folder / video.file_path).exists()):
                log.warning("File for %s is missing; re-queuing download", video.youtube_id)
                video.status = VideoStatus.PENDING
                video.file_path = None
        session.commit()
        try:
            apply(session, media_root, mapping, max_len)
        except OSError:
            log.exception("Failed to organize mapping %s", mapping.name)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_organizer.py tests/test_organizer_place.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/organizer.py tests/test_organizer_place.py
git commit -m "feat: organizer place/delete/reconcile"
```

---

### Task 8: Error classification and the yt-dlp client

**Files:**
- Create: `app/errors.py`, `app/ytdlp_client.py`
- Test: `tests/test_errors.py`, `tests/test_ytdlp_client.py`

**Interfaces:**
- Produces:
  - `ErrorKind` (`PERMANENT`, `RATE_LIMIT`, `TRANSIENT`), `classify(message: str) -> ErrorKind`
  - `class DownloadFailed(Exception)` with `.kind: ErrorKind`, `.message: str`; `class Cancelled(Exception)`
  - `@dataclass Entry(youtube_id: str, title: str, channel: str | None = None)`
  - `@dataclass VideoInfo(youtube_id, title, channel, description, upload_date, duration, thumbnail_url, artist, track)` with `VideoInfo.from_info(info: dict)`
  - `video_url(youtube_id) -> str`
  - `build_format(max_height: int, vcodec: str) -> str`
  - `common_opts(settings: dict, cookiefile: Path | None) -> dict`
  - `download_opts(settings: dict, staging_dir: Path, cookiefile: Path | None, progress_hook, pp_hook) -> dict`
  - `entries_from_info(info: dict) -> list[Entry]`
  - `fetch_entries(url: str, settings: dict, cookiefile: Path | None) -> list[Entry]` (blocking)
  - `collect_outputs(staging_dir: Path) -> tuple[Path | None, list[Path]]`
  - `download(youtube_id: str, settings: dict, staging_dir: Path, cookiefile: Path | None, on_progress: Callable[[dict], None], is_cancelled: Callable[[], bool]) -> tuple[VideoInfo, Path, list[Path]]` (blocking). Progress dicts have keys `stage` (`"downloading"|"processing"`), `percent`, `speed`, `eta`, `detail`.
  - `ytdlp_version() -> str`

- [ ] **Step 1: Write the failing error-classification tests**

`tests/test_errors.py`:
```python
import pytest

from app.errors import ErrorKind, classify


@pytest.mark.parametrize("message, kind", [
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: Video unavailable", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: This video has been removed by the uploader", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: The uploader has not made this video available in your country", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: This video is only available to Music Premium members", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: Join this channel to get access to members-only content", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: Sign in to confirm your age.", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot", ErrorKind.RATE_LIMIT),
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", ErrorKind.RATE_LIMIT),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", ErrorKind.TRANSIENT),
    ("ERROR: [youtube] abc: Premieres in 3 hours", ErrorKind.TRANSIENT),
    ("Connection reset by peer", ErrorKind.TRANSIENT),
])
def test_classify(message, kind):
    assert classify(message) == kind
```

- [ ] **Step 2: Implement `app/errors.py`**

```python
from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    PERMANENT = "permanent"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"


_RATE_LIMIT = (
    "http error 429", "too many requests", "not a bot", "rate-limited", "rate limited",
    "this content isn't available, try again later",
)
_TRANSIENT_OVERRIDES = ("premieres in", "live event will begin", "is not live yet")
_PERMANENT = (
    "private video", "video unavailable", "has been removed", "no longer available",
    "not made this video available in your country", "blocked it in your country",
    "not available in your country", "copyright", "members-only", "join this channel",
    "premium members", "account associated with this video has been terminated",
    "confirm your age", "inappropriate for some users", "does not exist",
)


def classify(message: str) -> ErrorKind:
    text = message.lower().replace("’", "'")
    if any(p in text for p in _RATE_LIMIT):
        return ErrorKind.RATE_LIMIT
    if any(p in text for p in _TRANSIENT_OVERRIDES):
        return ErrorKind.TRANSIENT
    if any(p in text for p in _PERMANENT):
        return ErrorKind.PERMANENT
    return ErrorKind.TRANSIENT
```

Run: `.venv/bin/pytest tests/test_errors.py -v`
Expected: all PASS

- [ ] **Step 3: Write the failing yt-dlp client tests**

`tests/test_ytdlp_client.py`:
```python
from pathlib import Path

import pytest
import yt_dlp

from app import settings as settings_mod
from app import ytdlp_client
from app.errors import ErrorKind
from app.ytdlp_client import Cancelled, DownloadFailed


def opts_for(**overrides):
    values = dict(settings_mod.DEFAULTS)
    values.update(overrides)
    return values


def test_build_format_any_codec():
    assert ytdlp_client.build_format(1080, "any") == "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"


def test_build_format_preferred_codec_first():
    fmt = ytdlp_client.build_format(720, "h264")
    assert fmt.startswith("bv*[height<=720][vcodec^=avc1]+ba/")
    assert fmt.endswith("/bv*+ba/b")


def test_download_opts_defaults(tmp_path):
    opts = ytdlp_client.download_opts(opts_for(), tmp_path, None, lambda d: None, lambda d: None)
    keys = [pp["key"] for pp in opts["postprocessors"]]
    assert keys == ["FFmpegThumbnailsConvertor", "FFmpegVideoRemuxer", "FFmpegMetadata"]
    assert opts["outtmpl"] == str(tmp_path / "media.%(ext)s")
    assert opts["merge_output_format"] == "mkv"
    assert opts["writethumbnail"] is True
    assert opts["sleep_interval_requests"] == 1.0
    assert "ratelimit" not in opts and "cookiefile" not in opts and "writesubtitles" not in opts


def test_download_opts_all_features(tmp_path):
    cookie = tmp_path / "cookies.txt"
    opts = ytdlp_client.download_opts(
        opts_for(subtitles="sidecar", subtitle_langs="en, de", sponsorblock="remove", ratelimit_kbps=500,
                 sleep_interval=5, max_sleep_interval=10, container="mp4"),
        tmp_path, cookie, lambda d: None, lambda d: None)
    keys = [pp["key"] for pp in opts["postprocessors"]]
    assert "FFmpegSubtitlesConvertor" in keys and "SponsorBlock" in keys and "ModifyChapters" in keys
    modify = next(pp for pp in opts["postprocessors"] if pp["key"] == "ModifyChapters")
    assert modify["remove_sponsor_segments"] == {"sponsor", "selfpromo", "interaction"}
    assert opts["subtitleslangs"] == ["en", "de"]
    assert opts["ratelimit"] == 500 * 1024
    assert opts["sleep_interval"] == 5 and opts["max_sleep_interval"] == 10
    assert opts["cookiefile"] == str(cookie)
    assert opts["merge_output_format"] == "mp4"


def test_download_opts_embed_subtitles(tmp_path):
    opts = ytdlp_client.download_opts(opts_for(subtitles="embed"), tmp_path, None, lambda d: None, lambda d: None)
    assert opts["postprocessors"][-1]["key"] == "FFmpegEmbedSubtitle"


def test_entries_single_video():
    entries = ytdlp_client.entries_from_info({"id": "abc", "title": "T", "channel": "C"})
    assert [(e.youtube_id, e.title, e.channel) for e in entries] == [("abc", "T", "C")]


def test_entries_skip_placeholders_and_tabs():
    info = {"_type": "playlist", "entries": [
        {"id": "a", "title": "A", "uploader": "U"},
        None,
        {"id": "p", "title": "[Private video]"},
        {"id": "d", "title": "[Deleted video]"},
        {"id": "tab", "title": "Videos", "ie_key": "YoutubeTab", "_type": "url"},
        {"id": "b", "title": None},
    ]}
    entries = ytdlp_client.entries_from_info(info)
    assert [(e.youtube_id, e.title, e.channel) for e in entries] == [("a", "A", "U"), ("b", "b", None)]


def test_collect_outputs(tmp_path):
    (tmp_path / "media.mkv").write_text("x" * 10)
    (tmp_path / "media.jpg").write_text("t")
    (tmp_path / "media.en.srt").write_text("s")
    (tmp_path / "media.f137.mp4.part").write_text("p")
    media, extras = ytdlp_client.collect_outputs(tmp_path)
    assert media == tmp_path / "media.mkv"
    assert sorted(p.name for p in extras) == ["media.en.srt", "media.jpg"]


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; behaviour is set per test via class attributes."""
    behaviour = "ok"

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download):
        staging = Path(self.opts["outtmpl"]).parent
        if FakeYDL.behaviour == "private":
            raise yt_dlp.utils.DownloadError("ERROR: [youtube] abc: Private video")
        for hook in self.opts["progress_hooks"]:
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "speed": 10.0, "eta": 5})
        (staging / "media.mkv").write_text("video")
        (staging / "media.jpg").write_text("thumb")
        return {"id": "abc", "title": "Title", "channel": "Chan", "upload_date": "20240101", "duration": 61.0,
                "description": "D", "thumbnail": "http://t", "artists": ["A1", "A2"], "track": "Song"}


@pytest.fixture
def fake_ydl(monkeypatch):
    FakeYDL.behaviour = "ok"
    monkeypatch.setattr(ytdlp_client.yt_dlp, "YoutubeDL", FakeYDL)
    return FakeYDL


def test_download_success(fake_ydl, tmp_path):
    events = []
    info, media, extras = ytdlp_client.download("abc", opts_for(), tmp_path / "stage", None, events.append, lambda: False)
    assert media.name == "media.mkv" and [p.name for p in extras] == ["media.jpg"]
    assert info.title == "Title" and info.artist == "A1, A2" and info.track == "Song" and info.duration == 61
    assert events[0] == {"stage": "downloading", "percent": 50.0, "speed": 10.0, "eta": 5, "detail": None}


def test_download_error_is_classified(fake_ydl, tmp_path):
    fake_ydl.behaviour = "private"
    with pytest.raises(DownloadFailed) as exc:
        ytdlp_client.download("abc", opts_for(), tmp_path / "stage", None, lambda d: None, lambda: False)
    assert exc.value.kind == ErrorKind.PERMANENT
    assert exc.value.message == "[youtube] abc: Private video"


def test_download_cancelled(fake_ydl, tmp_path):
    with pytest.raises(Cancelled):
        ytdlp_client.download("abc", opts_for(), tmp_path / "stage", None, lambda d: None, lambda: True)
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ytdlp_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.ytdlp_client'`

- [ ] **Step 5: Implement `app/ytdlp_client.py`**

```python
"""Thin, blocking wrapper over the yt-dlp Python API. Call from worker threads only."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from app.errors import ErrorKind, classify

log = logging.getLogger("yt_dlp")

MEDIA_EXTS = {".mkv", ".mp4", ".webm", ".m4v", ".mov"}
EXTRA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".srt", ".vtt", ".ass"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_PLACEHOLDER_TITLES = {"[Private video]", "[Deleted video]"}
_VCODEC_FILTER = {"h264": "[vcodec^=avc1]", "av1": "[vcodec^=av01]", "vp9": "[vcodec~='^vp0?9']"}


class DownloadFailed(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class Cancelled(Exception):
    pass


@dataclass
class Entry:
    youtube_id: str
    title: str
    channel: str | None = None


@dataclass
class VideoInfo:
    youtube_id: str
    title: str
    channel: str | None
    description: str | None
    upload_date: str | None
    duration: int | None
    thumbnail_url: str | None
    artist: str | None
    track: str | None

    @classmethod
    def from_info(cls, info: dict[str, Any]) -> VideoInfo:
        artists = info.get("artists")
        return cls(
            youtube_id=info["id"],
            title=info.get("title") or info["id"],
            channel=info.get("channel") or info.get("uploader"),
            description=info.get("description"),
            upload_date=info.get("upload_date"),
            duration=int(info["duration"]) if info.get("duration") else None,
            thumbnail_url=info.get("thumbnail"),
            artist=", ".join(artists) if artists else info.get("artist"),
            track=info.get("track"),
        )


class _Logger:
    """Routes yt-dlp output into Python logging (and so into the live log view)."""

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] ") or (msg.startswith("[download]") and "%" in msg):
            return
        log.info(msg)

    def info(self, msg: str) -> None:
        log.info(msg)

    def warning(self, msg: str) -> None:
        log.warning(msg)

    def error(self, msg: str) -> None:
        log.error(msg)


def video_url(youtube_id: str) -> str:
    return f"https://www.youtube.com/watch?v={youtube_id}"


def ytdlp_version() -> str:
    from yt_dlp.version import __version__

    return __version__


def _clean_message(message: str) -> str:
    return _ANSI.sub("", message).removeprefix("ERROR: ").strip()


def build_format(max_height: int, vcodec: str) -> str:
    height = f"[height<={max_height}]"
    chain = []
    if vcodec in _VCODEC_FILTER:
        chain.append(f"bv*{height}{_VCODEC_FILTER[vcodec]}+ba")
    chain += [f"bv*{height}+ba", f"b{height}", "bv*+ba", "b"]
    return "/".join(chain)


def common_opts(settings: dict[str, Any], cookiefile: Path | None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "noprogress": True,
        "logger": _Logger(),
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
    }
    if settings["sleep_requests"]:
        opts["sleep_interval_requests"] = settings["sleep_requests"]
    if cookiefile:
        opts["cookiefile"] = str(cookiefile)
    return opts


def download_opts(settings: dict[str, Any], staging_dir: Path, cookiefile: Path | None,
                  progress_hook: Callable[[dict], None], pp_hook: Callable[[dict], None]) -> dict[str, Any]:
    container = settings["container"]
    opts = common_opts(settings, cookiefile)
    opts.update({
        "format": build_format(settings["max_height"], settings["vcodec"]),
        "outtmpl": str(staging_dir / "media.%(ext)s"),
        "merge_output_format": container,
        "writethumbnail": True,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
    })
    if settings["ratelimit_kbps"]:
        opts["ratelimit"] = settings["ratelimit_kbps"] * 1024
    if settings["sleep_interval"]:
        opts["sleep_interval"] = settings["sleep_interval"]
        opts["max_sleep_interval"] = max(settings["max_sleep_interval"], settings["sleep_interval"])

    pps: list[dict[str, Any]] = [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}]
    if settings["subtitles"] != "off":
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = bool(settings["subtitle_auto"])
        opts["subtitleslangs"] = [lang.strip() for lang in settings["subtitle_langs"].split(",") if lang.strip()]
        if settings["subtitles"] == "sidecar":
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"})
    if settings["sponsorblock"] != "off":
        categories = {c.strip() for c in settings["sponsorblock_categories"].split(",") if c.strip()}
        pps.append({"key": "SponsorBlock", "categories": categories, "when": "after_filter"})
    pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": container})
    if settings["sponsorblock"] != "off":
        pps.append({
            "key": "ModifyChapters",
            "remove_sponsor_segments": categories if settings["sponsorblock"] == "remove" else set(),
            "sponsorblock_chapter_title": "[SponsorBlock]: %(category_names)l",
            "force_keyframes": False,
        })
    pps.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True})
    if settings["subtitles"] == "embed":
        pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    opts["postprocessors"] = pps
    return opts


def entries_from_info(info: dict[str, Any]) -> list[Entry]:
    if info.get("_type") in ("playlist", "multi_video") or "entries" in info:
        entries: list[Entry] = []
        for entry in info.get("entries") or []:
            if not entry or not entry.get("id"):
                continue
            if entry.get("ie_key") == "YoutubeTab" or entry.get("_type") == "playlist":
                continue
            title = entry.get("title") or entry["id"]
            if title in _PLACEHOLDER_TITLES:
                continue
            entries.append(Entry(entry["id"], title, entry.get("channel") or entry.get("uploader")))
        return entries
    return [Entry(info["id"], info.get("title") or info["id"], info.get("channel") or info.get("uploader"))]


def fetch_entries(url: str, settings: dict[str, Any], cookiefile: Path | None) -> list[Entry]:
    opts = common_opts(settings, cookiefile) | {"extract_flat": "in_playlist", "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.YoutubeDLError as exc:
        message = _clean_message(str(exc))
        raise DownloadFailed(classify(message), message) from exc
    return entries_from_info(info or {})


def collect_outputs(staging_dir: Path) -> tuple[Path | None, list[Path]]:
    files = [p for p in staging_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp"))]
    media = max((p for p in files if p.suffix.lower() in MEDIA_EXTS), key=lambda p: p.stat().st_size, default=None)
    extras = [p for p in files if p.suffix.lower() in EXTRA_EXTS]
    return media, extras


def _progress_event(d: dict[str, Any]) -> dict[str, Any]:
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    done = d.get("downloaded_bytes") or 0
    return {
        "stage": "downloading",
        "percent": round(done * 100 / total, 1) if total else None,
        "speed": d.get("speed"),
        "eta": d.get("eta"),
        "detail": None,
    }


def download(youtube_id: str, settings: dict[str, Any], staging_dir: Path, cookiefile: Path | None,
             on_progress: Callable[[dict], None], is_cancelled: Callable[[], bool]) -> tuple[VideoInfo, Path, list[Path]]:
    staging_dir.mkdir(parents=True, exist_ok=True)

    def progress_hook(d: dict[str, Any]) -> None:
        if is_cancelled():
            raise Cancelled()
        if d.get("status") == "downloading":
            on_progress(_progress_event(d))

    def pp_hook(d: dict[str, Any]) -> None:
        if is_cancelled():
            raise Cancelled()
        if d.get("status") == "started":
            on_progress({"stage": "processing", "percent": None, "speed": None, "eta": None,
                         "detail": d.get("postprocessor")})

    opts = download_opts(settings, staging_dir, cookiefile, progress_hook, pp_hook)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url(youtube_id), download=True)
    except Exception as exc:
        if is_cancelled() or isinstance(exc, Cancelled):
            raise Cancelled() from exc
        message = _clean_message(str(exc))
        kind = classify(message) if isinstance(exc, yt_dlp.utils.YoutubeDLError) else ErrorKind.TRANSIENT
        raise DownloadFailed(kind, message) from exc
    if is_cancelled():
        raise Cancelled()
    media, extras = collect_outputs(staging_dir)
    if media is None:
        raise DownloadFailed(ErrorKind.TRANSIENT, "Download finished but no media file was produced")
    return VideoInfo.from_info(info), media, extras
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_errors.py tests/test_ytdlp_client.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add app/errors.py app/ytdlp_client.py tests/test_errors.py tests/test_ytdlp_client.py
git commit -m "feat: yt-dlp client with format fallbacks and error classification"
```

---
### Task 9: Services: mapping and video mutations

**Files:**
- Create: `app/services.py`
- Test: `tests/test_services.py`

**Interfaces:**
- Consumes: `organizer.apply`, `organizer.ensure_under_root`, `organizer.ordered_videos`, `organizer.delete_video_files`, `organizer.delete_mapping_folder`, `organizer.PathError` (Tasks 6–7); `settings.get_all`, `settings.coerce_overrides` (Task 2)
- Produces:
  - `class ServiceError(ValueError)`
  - `@dataclass MappingInput(name: str, type: str, folder: str, playlist_url: str|None=None, show_title: str|None=None, enabled: bool=True, sync_interval_minutes: int|None=None, overrides: dict=field(default_factory=dict))`
  - `max_len(session) -> int`
  - `resolve_folder(media_root: Path, folder: str) -> Path`
  - `create_mapping(session, media_root, data: MappingInput) -> Mapping`
  - `update_mapping(session, media_root, mapping, data: MappingInput) -> Mapping`
  - `delete_mapping(session, media_root, mapping, delete_files: bool) -> None`
  - `reorder(session, media_root, mapping, ordered_ids: list[int]) -> None`
  - `set_season_break(session, media_root, mapping, video_id: int, on: bool) -> None`
  - `rename_video(session, media_root, video, *, title: str|None=None, artist: str|None=None, song: str|None=None) -> None`
  - `delete_video(session, media_root, video, ignore: bool) -> None`
  - `unignore(session, mapping, youtube_id: str) -> None`
  - `retry_video(session, video) -> None`
  - `mapping_stats(session) -> dict[int, dict[str, int]]`, `EMPTY_STATS: dict[str, int]`

- [ ] **Step 1: Write the failing tests**

`tests/test_services.py`:
```python
from pathlib import Path

import pytest
from sqlalchemy import select

from app import services
from app.models import IgnoredVideo, Mapping, MappingType, Video, VideoStatus
from app.services import MappingInput, ServiceError


def show_input(**kw):
    data = dict(name="Veritasium", type=MappingType.SHOW, folder="shows/Veritasium",
                playlist_url="https://www.youtube.com/playlist?list=PL1")
    data.update(kw)
    return MappingInput(**data)


def put_done(session, mapping, add_video, yid, title):
    folder = Path(mapping.folder)
    (folder / f"{yid}.mkv").write_text(f"media {yid}")
    return add_video(mapping, yid, title=title, status=VideoStatus.DONE, file_path=f"{yid}.mkv")


def test_create_mapping_resolves_relative_folder_and_writes_tvshow(session, config):
    mapping = services.create_mapping(session, config.media_root, show_input())
    folder = (config.media_root / "shows" / "Veritasium").resolve()
    assert mapping.folder == str(folder)
    assert mapping.show_title == "Veritasium"
    assert (folder / "tvshow.nfo").exists()


@pytest.mark.parametrize("folder", ["", "../outside", "/etc", ".", ".ytjs-tmp/x", "shows/../../x"])
def test_create_mapping_rejects_escaping_folders(session, config, folder):
    with pytest.raises(ServiceError):
        services.create_mapping(session, config.media_root, show_input(folder=folder))
    assert session.scalars(select(Mapping)).all() == []
    assert not (config.media_root.parent / "outside").exists()


def test_create_mapping_rejects_symlink_escape(session, config, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (config.media_root / "link").symlink_to(outside)
    with pytest.raises(ServiceError):
        services.create_mapping(session, config.media_root, show_input(folder="link/show"))


def test_create_mapping_rejects_overlap(session, config):
    services.create_mapping(session, config.media_root, show_input())
    for folder in ["shows/Veritasium", "shows", "shows/Veritasium/sub"]:
        with pytest.raises(ServiceError, match="overlaps"):
            services.create_mapping(session, config.media_root, show_input(name="Other", folder=folder))


@pytest.mark.parametrize("kw", [{"name": " "}, {"type": "movie"}, {"playlist_url": "ftp://x"},
                                {"sync_interval_minutes": 1}, {"overrides": {"max_height": "123"}}])
def test_create_mapping_validates_input(session, config, kw):
    with pytest.raises(ValueError):
        services.create_mapping(session, config.media_root, show_input(**kw))


def test_update_mapping_moves_folder_and_renames_show(session, config, add_video):
    mapping = services.create_mapping(session, config.media_root, show_input())
    put_done(session, mapping, add_video, "a", "Ep")
    services.update_mapping(session, config.media_root, mapping,
                            show_input(folder="shows/Veri", show_title="Veri"))
    new = (config.media_root / "shows" / "Veri").resolve()
    assert mapping.folder == str(new)
    assert not (config.media_root / "shows" / "Veritasium").exists()
    assert (new / "Season 01" / "Veri S01E01 Ep.mkv").read_text() == "media a"


def test_update_mapping_rejects_type_change(session, config):
    mapping = services.create_mapping(session, config.media_root, show_input())
    with pytest.raises(ServiceError):
        services.update_mapping(session, config.media_root, mapping, show_input(type=MappingType.MUSIC_VIDEO))


def test_delete_mapping_optionally_deletes_files(session, config):
    keep = services.create_mapping(session, config.media_root, show_input(name="Keep", folder="keep"))
    drop = services.create_mapping(session, config.media_root, show_input(name="Drop", folder="drop"))
    services.delete_mapping(session, config.media_root, keep, delete_files=False)
    services.delete_mapping(session, config.media_root, drop, delete_files=True)
    assert (config.media_root / "keep").exists()
    assert not (config.media_root / "drop").exists()
    assert session.scalars(select(Mapping)).all() == []


def test_reorder(session, config, make_mapping, add_video):
    mapping = make_mapping()
    a = put_done(session, mapping, add_video, "a", "A")
    b = put_done(session, mapping, add_video, "b", "B")
    services.reorder(session, config.media_root, mapping, [b.id, a.id])
    assert (a.position, b.position) == (2, 1)
    assert (Path(mapping.folder) / "Season 01" / "My Show S01E01 B.mkv").exists()
    with pytest.raises(ServiceError):
        services.reorder(session, config.media_root, mapping, [a.id])


def test_season_break_toggle(session, config, make_mapping, add_video):
    mapping = make_mapping()
    a = put_done(session, mapping, add_video, "a", "A")
    b = put_done(session, mapping, add_video, "b", "B")
    with pytest.raises(ServiceError):
        services.set_season_break(session, config.media_root, mapping, a.id, True)
    services.set_season_break(session, config.media_root, mapping, b.id, True)
    services.set_season_break(session, config.media_root, mapping, b.id, True)  # idempotent
    assert len(mapping.season_breaks) == 1
    assert (Path(mapping.folder) / "Season 02" / "My Show S02E01 B.mkv").exists()
    services.set_season_break(session, config.media_root, mapping, b.id, False)
    assert mapping.season_breaks == []


def test_rename_show_and_music(session, config, make_mapping, add_video):
    show = make_mapping()
    video = put_done(session, show, add_video, "a", "A")
    services.rename_video(session, config.media_root, video, title="  Better  ")
    assert video.custom_title == "Better" and video.user_edited
    assert (Path(show.folder) / "Season 01" / "My Show S01E01 Better.mkv").exists()
    services.rename_video(session, config.media_root, video, title="")
    assert video.custom_title is None

    music = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    mv = put_done(session, music, add_video, "m", "x")
    services.rename_video(session, config.media_root, mv, artist="Queen", song="Innuendo")
    assert (Path(music.folder) / "Queen - Innuendo.mkv").exists()
    with pytest.raises(ServiceError):
        services.rename_video(session, config.media_root, mv, artist="", song="x")


def test_delete_video_moves_break_ignores_and_renumbers(session, config, make_mapping, add_video):
    mapping = make_mapping(playlist_url="https://youtube.com/playlist?list=1")
    a = put_done(session, mapping, add_video, "a", "A")
    b = put_done(session, mapping, add_video, "b", "B")
    c = put_done(session, mapping, add_video, "c", "C")
    services.set_season_break(session, config.media_root, mapping, b.id, True)
    services.delete_video(session, config.media_root, b, ignore=True)
    assert [v.youtube_id for v in sorted(mapping.videos, key=lambda v: v.position)] == ["a", "c"]
    assert [v.position for v in sorted(mapping.videos, key=lambda v: v.position)] == [1, 2]
    assert [br.video_id for br in mapping.season_breaks] == [c.id]
    assert [i.youtube_id for i in mapping.ignored] == ["b"]
    folder = Path(mapping.folder)
    assert not any("B" in p.name for p in folder.rglob("*.mkv"))
    assert (folder / "Season 02" / "My Show S02E01 C.mkv").exists()
    assert session.get(Video, a.id) is not None


def test_delete_video_without_playlist_never_ignores(session, config, make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "a")
    services.delete_video(session, config.media_root, video, ignore=True)
    assert mapping.ignored == []


def test_unignore_and_retry(session, config, make_mapping, add_video):
    mapping = make_mapping()
    mapping.ignored.append(IgnoredVideo(youtube_id="z", title="Z"))
    session.commit()
    services.unignore(session, mapping, "z")
    assert mapping.ignored == []
    video = add_video(mapping, "f", status=VideoStatus.FAILED, attempts=3, error="boom")
    services.retry_video(session, video)
    assert (video.status, video.attempts, video.error, video.next_retry_at) == (VideoStatus.PENDING, 0, None, None)


def test_mapping_stats(session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "a")
    add_video(mapping, "b", status=VideoStatus.DONE)
    add_video(mapping, "c", status=VideoStatus.DONE)
    stats = services.mapping_stats(session)[mapping.id]
    assert stats["done"] == 2 and stats["pending"] == 1 and stats["total"] == 3 and stats["failed"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_services.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services'`

- [ ] **Step 3: Implement `app/services.py`**

```python
"""Mapping/video mutations. Each mutation updates the DB, then lets the organizer fix the disk."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import organizer
from app import settings as settings_mod
from app.models import IgnoredVideo, Mapping, MappingType, SeasonBreak, Video, VideoStatus


class ServiceError(ValueError):
    pass


@dataclass
class MappingInput:
    name: str
    type: str
    folder: str
    playlist_url: str | None = None
    show_title: str | None = None
    enabled: bool = True
    sync_interval_minutes: int | None = None
    overrides: dict[str, Any] = field(default_factory=dict)


EMPTY_STATS: dict[str, int] = {s.value: 0 for s in VideoStatus} | {"total": 0}


def max_len(session: Session) -> int:
    return settings_mod.get_all(session)["max_filename_length"]


def _apply(session: Session, media_root: Path, mapping: Mapping) -> None:
    organizer.apply(session, media_root, mapping, max_len(session))


def resolve_folder(media_root: Path, folder: str) -> Path:
    raw = folder.strip()
    if not raw:
        raise ServiceError("Folder is required")
    path = Path(raw)
    if not path.is_absolute():
        path = media_root / path
    try:
        return organizer.ensure_under_root(media_root, path)
    except organizer.PathError as exc:
        raise ServiceError(str(exc)) from exc


def _nested(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def _check_overlap(session: Session, folder: Path, exclude_id: int | None = None) -> None:
    for other in session.scalars(select(Mapping)):
        if other.id != exclude_id and _nested(Path(other.folder).resolve(), folder):
            raise ServiceError(f"Folder overlaps with mapping '{other.name}'")


def _validate(data: MappingInput) -> dict[str, Any]:
    if not data.name.strip():
        raise ServiceError("Name is required")
    if data.type not in (MappingType.SHOW, MappingType.MUSIC_VIDEO):
        raise ServiceError("Type must be show or music_video")
    if data.playlist_url and not data.playlist_url.startswith(("http://", "https://")):
        raise ServiceError("Playlist URL must start with http:// or https://")
    if data.sync_interval_minutes is not None and data.sync_interval_minutes < 5:
        raise ServiceError("Sync interval must be at least 5 minutes")
    return settings_mod.coerce_overrides(data.overrides or {})


def _assign(mapping: Mapping, data: MappingInput, overrides: dict[str, Any]) -> None:
    mapping.name = data.name.strip()
    mapping.playlist_url = (data.playlist_url or "").strip() or None
    mapping.show_title = ((data.show_title or "").strip() or mapping.name) if data.type == MappingType.SHOW else None
    mapping.enabled = data.enabled
    mapping.sync_interval_minutes = data.sync_interval_minutes
    mapping.overrides = overrides


def create_mapping(session: Session, media_root: Path, data: MappingInput) -> Mapping:
    overrides = _validate(data)
    folder = resolve_folder(media_root, data.folder)
    _check_overlap(session, folder)
    folder.mkdir(parents=True, exist_ok=True)
    mapping = Mapping(type=data.type, folder=str(folder))
    _assign(mapping, data, overrides)
    session.add(mapping)
    session.commit()
    _apply(session, media_root, mapping)
    return mapping


def update_mapping(session: Session, media_root: Path, mapping: Mapping, data: MappingInput) -> Mapping:
    overrides = _validate(data)
    if data.type != mapping.type:
        raise ServiceError("Mapping type cannot be changed")
    folder = resolve_folder(media_root, data.folder)
    _check_overlap(session, folder, exclude_id=mapping.id)
    old = Path(mapping.folder).resolve()
    if folder != old:
        if _nested(folder, old):
            raise ServiceError("The new folder cannot be inside the old one (or the other way round)")
        if folder.exists() and any(folder.iterdir()):
            raise ServiceError("The new folder already exists and is not empty")
        folder.parent.mkdir(parents=True, exist_ok=True)
        if folder.exists():
            folder.rmdir()
        if old.exists():
            shutil.move(str(old), str(folder))
        else:
            folder.mkdir(parents=True)
        mapping.folder = str(folder)
    _assign(mapping, data, overrides)
    session.commit()
    _apply(session, media_root, mapping)
    return mapping


def delete_mapping(session: Session, media_root: Path, mapping: Mapping, delete_files: bool) -> None:
    folder = mapping.folder
    session.delete(mapping)
    session.commit()
    if delete_files:
        organizer.delete_mapping_folder(media_root, folder)


def _get_video(mapping: Mapping, video_id: int) -> Video:
    for video in mapping.videos:
        if video.id == video_id:
            return video
    raise ServiceError("Video does not belong to this mapping")


def reorder(session: Session, media_root: Path, mapping: Mapping, ordered_ids: list[int]) -> None:
    current = {v.id: v for v in mapping.videos}
    if sorted(ordered_ids) != sorted(current):
        raise ServiceError("The new order must contain every video exactly once")
    for position, video_id in enumerate(ordered_ids, start=1):
        current[video_id].position = position
    session.commit()
    _apply(session, media_root, mapping)


def set_season_break(session: Session, media_root: Path, mapping: Mapping, video_id: int, on: bool) -> None:
    video = _get_video(mapping, video_id)
    existing = next((b for b in mapping.season_breaks if b.video_id == video_id), None)
    if on and existing is None:
        if video is organizer.ordered_videos(mapping)[0]:
            raise ServiceError("Season 1 always starts at the first video")
        mapping.season_breaks.append(SeasonBreak(video_id=video_id))
    elif not on and existing is not None:
        mapping.season_breaks.remove(existing)
    session.commit()
    _apply(session, media_root, mapping)


def rename_video(session: Session, media_root: Path, video: Video, *, title: str | None = None,
                 artist: str | None = None, song: str | None = None) -> None:
    mapping = video.mapping
    if mapping.type == MappingType.SHOW:
        video.custom_title = (title or "").strip() or None
    else:
        artist, song = (artist or "").strip(), (song or "").strip()
        if not artist or not song:
            raise ServiceError("Artist and song are both required")
        video.artist, video.song = artist, song
    video.user_edited = True
    session.commit()
    _apply(session, media_root, mapping)


def delete_video(session: Session, media_root: Path, video: Video, ignore: bool) -> None:
    mapping = video.mapping
    ordered = organizer.ordered_videos(mapping)
    brk = next((b for b in mapping.season_breaks if b.video_id == video.id), None)
    if brk is not None:
        mapping.season_breaks.remove(brk)
        index = ordered.index(video)
        following = ordered[index + 1] if index + 1 < len(ordered) else None
        if following is not None and all(b.video_id != following.id for b in mapping.season_breaks):
            mapping.season_breaks.append(SeasonBreak(video_id=following.id))
        session.flush()
    organizer.delete_video_files(media_root, mapping, video)
    if ignore and mapping.playlist_url and all(i.youtube_id != video.youtube_id for i in mapping.ignored):
        mapping.ignored.append(IgnoredVideo(youtube_id=video.youtube_id, title=video.display_title))
    mapping.videos.remove(video)
    for position, remaining in enumerate(organizer.ordered_videos(mapping), start=1):
        remaining.position = position
    session.commit()
    _apply(session, media_root, mapping)


def unignore(session: Session, mapping: Mapping, youtube_id: str) -> None:
    for item in list(mapping.ignored):
        if item.youtube_id == youtube_id:
            mapping.ignored.remove(item)
    session.commit()


def retry_video(session: Session, video: Video) -> None:
    video.status = VideoStatus.PENDING
    video.attempts = 0
    video.error = None
    video.next_retry_at = None
    session.commit()


def mapping_stats(session: Session) -> dict[int, dict[str, int]]:
    stats: dict[int, dict[str, int]] = {}
    rows = session.execute(
        select(Video.mapping_id, Video.status, func.count()).group_by(Video.mapping_id, Video.status)
    )
    for mapping_id, status, count in rows:
        entry = stats.setdefault(mapping_id, dict(EMPTY_STATS))
        entry[status] = count
        entry["total"] += count
    return stats
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_services.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/services.py tests/test_services.py
git commit -m "feat: services for mapping and video edits"
```

---

### Task 10: Realtime plumbing: event bus, notifier, log buffer, templating

**Files:**
- Create: `app/activity.py`, `app/events.py`, `app/logbuffer.py`, `app/templating.py`, `app/notify.py`
- Create: `app/templates/partials/progress.html`, `app/templates/partials/activity.html`, `app/templates/partials/log_line.html`
- Test: `tests/test_events.py`

**Interfaces:**
- Produces:
  - `activity.ActiveItem(video_id: int, mapping_id: int, title: str, mapping_name: str, stage: str="starting", percent: float|None=None, speed: float|None=None, eta: int|None=None, detail: str|None=None)`
  - `activity.ActivitySnapshot(paused=False, cooldown_until=None, cooldown_reason=None, next_download_at=None, active=[], queued=0, restart_pending=False)`
  - `events.Event(name: str, data: str)`, `events.EventBus` with `bind(loop)`, `subscribe() -> asyncio.Queue[Event]`, `unsubscribe(q)`, `publish(name, data)` (loop thread only), `publish_threadsafe(name, data)` (any thread), `events.format_sse(event) -> str`
  - `logbuffer.LogBuffer(capacity=500)` with `.lines: deque[str]` and `.listener: Callable[[str], None] | None`; `logbuffer.setup_logging(config, buffer) -> None`
  - `templating.templates` (a `Jinja2Templates` instance) with filters `localtime`, `isoutc`, `speed`, `duration`
  - `notify.Notifier(bus, env)` with methods `progress(video_id, item: ActiveItem)`, `video_changed(video_id, mapping_id)`, `list_changed(mapping_id)`, `mapping_changed(mapping_id)`, `sync_status(mapping_id, text)`, `activity(snapshot)`, `log_line(line)`
- SSE event names: `progress-{video_id}` (HTML), `video-{video_id}` (trigger), `list-{mapping_id}` (trigger), `mapping-{mapping_id}` (trigger), `sync-{mapping_id}` (escaped text), `activity` (HTML), `log` (HTML)

- [ ] **Step 1: Write the failing tests**

`tests/test_events.py`:
```python
import asyncio
import logging
from datetime import datetime

from app.activity import ActiveItem, ActivitySnapshot
from app.events import Event, EventBus, format_sse
from app.logbuffer import LogBuffer
from app.notify import Notifier
from app.templating import duration, speed, templates


async def test_publish_reaches_subscribers_and_unsubscribe_stops_it():
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    q1, q2 = bus.subscribe(), bus.subscribe()
    bus.publish("x", "1")
    assert (await q1.get()).name == "x" and (await q2.get()).data == "1"
    bus.unsubscribe(q1)
    bus.publish("y", "2")
    assert q1.empty() and (await q2.get()).name == "y"


async def test_full_queue_drops_events_instead_of_blocking():
    bus = EventBus(maxsize=1)
    bus.bind(asyncio.get_running_loop())
    q = bus.subscribe()
    bus.publish("a", "1")
    bus.publish("b", "2")
    assert q.qsize() == 1


async def test_publish_threadsafe_from_worker_thread():
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    q = bus.subscribe()
    await asyncio.to_thread(bus.publish_threadsafe, "t", "data")
    event = await asyncio.wait_for(q.get(), 1)
    assert event == Event("t", "data")


def test_format_sse_multiline():
    assert format_sse(Event("e", "<a>\n<b>")) == "event: e\ndata: <a>\ndata: <b>\n\n"


async def test_notifier_renders_progress_and_activity():
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    q = bus.subscribe()
    notifier = Notifier(bus, templates.env)
    item = ActiveItem(video_id=7, mapping_id=1, title="T", mapping_name="M", stage="downloading",
                      percent=42.5, speed=2048.0, eta=65)
    notifier.progress(7, item)
    event = await q.get()
    assert event.name == "progress-7" and "width: 42.5%" in event.data and "2.0 KiB/s" in event.data
    notifier.activity(ActivitySnapshot(active=[item], queued=3, cooldown_until=datetime(2026, 1, 1),
                                       cooldown_reason="429"))
    event = await q.get()
    assert event.name == "activity" and "3 waiting" in event.data and "rate-limited" in event.data
    notifier.sync_status(1, "<b>bad</b>")
    assert (await q.get()).data == "&lt;b&gt;bad&lt;/b&gt;"
    notifier.video_changed(7, 1)
    assert [(await q.get()).name, (await q.get()).name] == ["video-7", "mapping-1"]


def test_log_buffer_captures_and_notifies():
    buffer = LogBuffer(capacity=2)
    seen = []
    buffer.listener = seen.append
    logger = logging.getLogger("test-buffer")
    logger.addHandler(buffer)
    logger.setLevel(logging.INFO)
    for i in range(3):
        logger.info("line %s", i)
    logger.removeHandler(buffer)
    assert list(buffer.lines) == ["line 1", "line 2"]
    assert seen == ["line 0", "line 1", "line 2"]


def test_filters():
    assert speed(None) == "" and speed(512) == "512.0 B/s" and speed(3 * 1024 * 1024) == "3.0 MiB/s"
    assert duration(None) == "" and duration(65) == "1:05" and duration(3725) == "1:02:05"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.activity'`

- [ ] **Step 3: Implement `app/activity.py` and `app/events.py`**

`app/activity.py`:
```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ActiveItem:
    video_id: int
    mapping_id: int
    title: str
    mapping_name: str
    stage: str = "starting"
    percent: float | None = None
    speed: float | None = None
    eta: int | None = None
    detail: str | None = None


@dataclass
class ActivitySnapshot:
    paused: bool = False
    cooldown_until: datetime | None = None
    cooldown_reason: str | None = None
    next_download_at: datetime | None = None
    active: list[ActiveItem] = field(default_factory=list)
    queued: int = 0
    restart_pending: bool = False
```

`app/events.py`:
```python
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    name: str
    data: str


class EventBus:
    """In-process pub/sub feeding SSE clients. Slow clients lose events rather than block."""

    def __init__(self, maxsize: int = 500) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._maxsize = maxsize

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def publish(self, name: str, data: str) -> None:
        """Must run on the event loop thread; use publish_threadsafe from anywhere else."""
        event = Event(name, data)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def publish_threadsafe(self, name: str, data: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.publish(name, data)
        else:
            loop.call_soon_threadsafe(self.publish, name, data)


def format_sse(event: Event) -> str:
    lines = event.data.splitlines() or [""]
    return f"event: {event.name}\n" + "".join(f"data: {line}\n" for line in lines) + "\n"
```

- [ ] **Step 4: Implement `app/logbuffer.py` and `app/templating.py`**

`app/logbuffer.py`:
```python
from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from logging.handlers import RotatingFileHandler

from app.config import AppConfig


class LogBuffer(logging.Handler):
    """Keeps the last N formatted log lines and forwards each new line to a listener."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)
        self.listener: Callable[[str], None] | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)
            return
        self.lines.append(line)
        if self.listener is not None:
            self.listener(line)


def setup_logging(config: AppConfig, buffer: LogBuffer) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_ytjs", False):
            root.removeHandler(handler)
            handler.close()
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(config.logs_dir / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    for handler in (file_handler, logging.StreamHandler(), buffer):
        handler.setFormatter(formatter)
        handler._ytjs = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
```

`app/templating.py`:
```python
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.timeutil import to_local

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def localtime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    local = to_local(dt)
    return local.strftime(fmt) if local else "never"


def isoutc(dt: datetime | None) -> str:
    return dt.replace(tzinfo=UTC).isoformat() if dt else ""


def speed(bytes_per_second: float | None) -> str:
    if not bytes_per_second:
        return ""
    value = float(bytes_per_second)
    for unit in ("B/s", "KiB/s", "MiB/s"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB/s"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


templates.env.filters.update(localtime=localtime, isoutc=isoutc, speed=speed, duration=duration)
```

- [ ] **Step 5: Implement `app/notify.py` and the partial templates**

`app/notify.py`:
```python
from __future__ import annotations

from jinja2 import Environment
from markupsafe import escape

from app.activity import ActiveItem, ActivitySnapshot
from app.events import EventBus


class Notifier:
    """Turns domain changes into SSE events. Safe to call from any thread."""

    def __init__(self, bus: EventBus, env: Environment) -> None:
        self.bus = bus
        self.env = env

    def _render(self, template: str, **context) -> str:
        return self.env.get_template(template).render(**context)

    def progress(self, video_id: int, item: ActiveItem) -> None:
        self.bus.publish_threadsafe(f"progress-{video_id}", self._render("partials/progress.html", p=item))

    def video_changed(self, video_id: int, mapping_id: int) -> None:
        self.bus.publish_threadsafe(f"video-{video_id}", "1")
        self.bus.publish_threadsafe(f"mapping-{mapping_id}", "1")

    def list_changed(self, mapping_id: int) -> None:
        self.bus.publish_threadsafe(f"list-{mapping_id}", "1")
        self.bus.publish_threadsafe(f"mapping-{mapping_id}", "1")

    def mapping_changed(self, mapping_id: int) -> None:
        self.bus.publish_threadsafe(f"mapping-{mapping_id}", "1")

    def sync_status(self, mapping_id: int, text: str) -> None:
        self.bus.publish_threadsafe(f"sync-{mapping_id}", str(escape(text)))

    def activity(self, snapshot: ActivitySnapshot) -> None:
        self.bus.publish_threadsafe("activity", self._render("partials/activity.html", a=snapshot))

    def log_line(self, line: str) -> None:
        self.bus.publish_threadsafe("log", self._render("partials/log_line.html", line=line))
```

`app/templates/partials/progress.html`:
```html
{% if p.stage == "downloading" %}
<div class="progress"><div class="bar" style="width: {{ p.percent or 0 }}%"></div></div>
<span class="progress-text">{% if p.percent is not none %}{{ "%.1f"|format(p.percent) }}%{% else %}Downloading{% endif %}{% if p.speed %} · {{ p.speed|speed }}{% endif %}{% if p.eta %} · ETA {{ p.eta|duration }}{% endif %}</span>
{% else %}
<div class="progress indeterminate"><div class="bar"></div></div>
<span class="progress-text">{{ "Processing" if p.stage == "processing" else "Starting" }}{% if p.detail %} · {{ p.detail }}{% endif %}…</span>
{% endif %}
```

`app/templates/partials/activity.html`:
```html
{% if a.restart_pending %}
<div class="banner info">yt-dlp was updated. The app restarts as soon as no download is running.</div>
{% endif %}
{% if a.cooldown_until %}
<div class="banner warn">
  Downloads paused: YouTube rate-limited us{% if a.cooldown_reason %} ({{ a.cooldown_reason }}){% endif %}.
  Resuming at {{ a.cooldown_until|localtime("%H:%M") }} (in <span data-countdown="{{ a.cooldown_until|isoutc }}"></span>).
  <button class="btn small" hx-post="/queue/resume" hx-swap="none">Resume now</button>
</div>
{% elif a.paused %}
<div class="banner warn">Queue paused. <button class="btn small" hx-post="/queue/resume" hx-swap="none">Resume</button></div>
{% endif %}
<div class="activity-bar">
  <span class="activity-label">Queue</span>
  <span>{{ a.queued }} waiting</span>
  {% if not a.active and a.queued and a.next_download_at and not a.paused and not a.cooldown_until %}
  <span class="muted">· next download in <span data-countdown="{{ a.next_download_at|isoutc }}"></span></span>
  {% endif %}
  {% if not a.paused and not a.cooldown_until %}
  <button class="btn small ghost push-right" hx-post="/queue/pause" hx-swap="none">Pause queue</button>
  {% endif %}
</div>
{% for item in a.active %}
<div class="activity-item">
  <div class="activity-title"><a href="/mappings/{{ item.mapping_id }}">{{ item.mapping_name }}</a> · {{ item.title }}</div>
  <div class="progress-wrap" sse-swap="progress-{{ item.video_id }}">{% with p = item %}{% include "partials/progress.html" %}{% endwith %}</div>
</div>
{% endfor %}
```

`app/templates/partials/log_line.html`:
```html
<div class="log-line">{{ line }}</div>
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_events.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add app/activity.py app/events.py app/logbuffer.py app/templating.py app/notify.py app/templates tests/test_events.py
git commit -m "feat: SSE event bus, notifier, log buffer and template filters"
```

---

### Task 11: Playlist sync and manual add

**Files:**
- Create: `app/sync.py`
- Modify: `tests/conftest.py` (add `FakeWorker` + `fake_worker` fixture)
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `ytdlp_client.Entry`, `ytdlp_client.fetch_entries`, `ytdlp_client.DownloadFailed` (Task 8); `mvparse.parse_artist_song` (Task 4); `settings.resolve` (Task 2); `services.ServiceError` (Task 9); Notifier methods (Task 10). Worker duck type: `kick()`, `enter_cooldown(reason: str)`.
- Produces:
  - `add_entries(session, mapping, entries: list[Entry], source: str) -> list[Video]`
  - `class Syncer(session_factory, config, worker, notifier, fetch=ytdlp_client.fetch_entries)` with `async sync(mapping_id: int) -> int | None` (number added; `None` if skipped or failed), `async add_url(mapping_id: int, url: str) -> int` (raises `ServiceError` or `DownloadFailed`), and attribute `running: set[int]`

- [ ] **Step 1: Add `FakeWorker` to `tests/conftest.py`**

Append:
```python
class FakeWorker:
    def __init__(self) -> None:
        self.kicks = 0
        self.cooldowns: list[str] = []
        self.blocked = False
        self.restart_pending = False
        self.cancelled: list[int] = []
        self.cancelled_mappings: list[int] = []
        self.paused = False
        self.cooldown_until = None

    def kick(self) -> None:
        self.kicks += 1

    def enter_cooldown(self, reason: str) -> None:
        self.cooldowns.append(reason)

    def is_blocked(self) -> bool:
        return self.blocked

    def cancel(self, video_id: int) -> None:
        self.cancelled.append(video_id)

    def cancel_mapping(self, mapping_id: int) -> None:
        self.cancelled_mappings.append(mapping_id)


@pytest.fixture
def fake_worker() -> FakeWorker:
    return FakeWorker()
```

- [ ] **Step 2: Write the failing tests**

`tests/test_sync.py`:
```python
import pytest

from app.errors import ErrorKind
from app.models import IgnoredVideo, Mapping, MappingType, Video
from app.services import ServiceError
from app.sync import Syncer, add_entries
from app.ytdlp_client import DownloadFailed, Entry

URL = "https://www.youtube.com/playlist?list=PL1"


def test_add_entries_is_additive_and_appends(session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "old")
    added = add_entries(session, mapping, [Entry("old", "Old"), Entry("new1", "N1"), Entry("new2", "N2")], "sync")
    assert [v.youtube_id for v in added] == ["new1", "new2"]
    assert [(v.youtube_id, v.position) for v in sorted(mapping.videos, key=lambda v: v.position)] == [
        ("old", 1), ("new1", 2), ("new2", 3)]
    assert all(v.source == "sync" for v in added)


def test_add_entries_dedupes_within_batch(session, make_mapping):
    mapping = make_mapping()
    added = add_entries(session, mapping, [Entry("a", "A"), Entry("a", "A again")], "sync")
    assert len(added) == 1 and len(mapping.videos) == 1


def test_sync_skips_ignored_but_manual_add_unignores(session, make_mapping):
    mapping = make_mapping()
    mapping.ignored.append(IgnoredVideo(youtube_id="x", title="X"))
    session.commit()
    assert add_entries(session, mapping, [Entry("x", "X")], "sync") == []
    added = add_entries(session, mapping, [Entry("x", "X")], "manual")
    assert [v.youtube_id for v in added] == ["x"] and mapping.ignored == []


def test_music_entries_get_parsed_artist_song(session, make_mapping):
    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    [video] = add_entries(session, mapping, [Entry("m", "Queen - Innuendo (Official Video)", "Queen Official")], "sync")
    assert (video.artist, video.song) == ("Queen", "Innuendo")


async def test_syncer_adds_videos_and_notifies(session, session_factory, config, notifier, fake_worker, make_mapping):
    mapping = make_mapping(playlist_url=URL)
    syncer = Syncer(session_factory, config, fake_worker, notifier,
                    fetch=lambda url, opts, cookie: [Entry("a", "A"), Entry("b", "B")])
    assert await syncer.sync(mapping.id) == 2
    session.expire_all()
    reloaded = session.get(Mapping, mapping.id)
    assert len(reloaded.videos) == 2 and reloaded.last_synced_at is not None and reloaded.last_sync_error is None
    assert fake_worker.kicks == 1
    assert ("list_changed", (mapping.id,)) in notifier.calls
    assert notifier.calls[-2] == ("sync_status", (mapping.id, "2 new video(s)"))


async def test_syncer_records_failure_and_cools_down_on_rate_limit(session, session_factory, config, notifier,
                                                                  fake_worker, make_mapping):
    mapping = make_mapping(playlist_url=URL)

    def fetch(url, opts, cookie):
        raise DownloadFailed(ErrorKind.RATE_LIMIT, "HTTP Error 429")

    syncer = Syncer(session_factory, config, fake_worker, notifier, fetch=fetch)
    assert await syncer.sync(mapping.id) is None
    session.expire_all()
    reloaded = session.get(Mapping, mapping.id)
    assert reloaded.last_sync_error == "HTTP Error 429" and reloaded.last_synced_at is not None
    assert fake_worker.cooldowns == ["HTTP Error 429"]
    assert not syncer.running


async def test_syncer_ignores_manual_mappings(session_factory, config, notifier, fake_worker, make_mapping):
    mapping = make_mapping()
    syncer = Syncer(session_factory, config, fake_worker, notifier, fetch=lambda *a: pytest.fail("no fetch"))
    assert await syncer.sync(mapping.id) is None


async def test_add_url(session, session_factory, config, notifier, fake_worker, make_mapping):
    mapping = make_mapping()
    syncer = Syncer(session_factory, config, fake_worker, notifier, fetch=lambda url, opts, cookie: [Entry("v", "V")])
    assert await syncer.add_url(mapping.id, " https://youtu.be/v ") == 1
    session.expire_all()
    assert session.get(Mapping, mapping.id).videos[0].source == "manual"
    with pytest.raises(ServiceError):
        await syncer.add_url(mapping.id, "not a url")
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.sync'`

- [ ] **Step 4: Implement `app/sync.py`**

```python
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.orm import Session

from app import settings as settings_mod
from app import ytdlp_client
from app.errors import ErrorKind
from app.models import Mapping, MappingType, Video, VideoStatus
from app.mvparse import parse_artist_song
from app.services import ServiceError
from app.timeutil import utcnow
from app.ytdlp_client import DownloadFailed, Entry

log = logging.getLogger(__name__)


def add_entries(session: Session, mapping: Mapping, entries: list[Entry], source: str) -> list[Video]:
    """Append unseen entries at the end. Sync skips ignored ids; a manual add un-ignores them."""
    existing = {v.youtube_id for v in mapping.videos}
    ignored = {i.youtube_id: i for i in mapping.ignored}
    next_position = max((v.position for v in mapping.videos), default=0) + 1
    added: list[Video] = []
    for entry in entries:
        if entry.youtube_id in existing:
            continue
        if entry.youtube_id in ignored:
            if source == "sync":
                continue
            mapping.ignored.remove(ignored.pop(entry.youtube_id))
        video = Video(youtube_id=entry.youtube_id, yt_title=entry.title, channel=entry.channel,
                      position=next_position, source=source, status=VideoStatus.PENDING)
        if mapping.type == MappingType.MUSIC_VIDEO:
            video.artist, video.song = parse_artist_song(entry.title, entry.channel)
        mapping.videos.append(video)
        existing.add(entry.youtube_id)
        next_position += 1
        added.append(video)
    session.commit()
    return added


class Syncer:
    def __init__(self, session_factory, config, worker, notifier, fetch=ytdlp_client.fetch_entries) -> None:
        self.session_factory = session_factory
        self.config = config
        self.worker = worker
        self.notifier = notifier
        self._fetch = fetch
        self._lock = asyncio.Lock()
        self.running: set[int] = set()

    async def sync(self, mapping_id: int) -> int | None:
        async with self._lock:
            with self.session_factory() as session:
                mapping = session.get(Mapping, mapping_id)
                if mapping is None or not mapping.playlist_url:
                    return None
                url = mapping.playlist_url
                opts = settings_mod.resolve(session, mapping)
                name = mapping.name
            self.running.add(mapping_id)
            self.notifier.sync_status(mapping_id, "Checking playlist…")
            log.info("Syncing %s", name)
            try:
                entries = await asyncio.to_thread(self._fetch, url, opts, self.config.cookiefile())
            except DownloadFailed as exc:
                log.warning("Sync of %s failed: %s", name, exc.message)
                self._record(mapping_id, exc.message)
                if exc.kind == ErrorKind.RATE_LIMIT:
                    self.worker.enter_cooldown(exc.message)
                self.notifier.sync_status(mapping_id, f"Sync failed: {exc.message}")
                return None
            finally:
                self.running.discard(mapping_id)
            with self.session_factory() as session:
                mapping = session.get(Mapping, mapping_id)
                if mapping is None:
                    return None
                added = add_entries(session, mapping, entries, source="sync")
                mapping.last_synced_at = utcnow()
                mapping.last_sync_error = None
                session.commit()
            log.info("Sync of %s found %d new video(s)", name, len(added))
            self.notifier.sync_status(mapping_id, f"{len(added)} new video(s)" if added else "Up to date")
            if added:
                self.notifier.list_changed(mapping_id)
                self.worker.kick()
            else:
                self.notifier.mapping_changed(mapping_id)
            return len(added)

    def _record(self, mapping_id: int, error: str) -> None:
        with self.session_factory() as session:
            mapping = session.get(Mapping, mapping_id)
            if mapping is not None:
                mapping.last_synced_at = utcnow()
                mapping.last_sync_error = error
                session.commit()
        self.notifier.mapping_changed(mapping_id)

    async def add_url(self, mapping_id: int, url: str) -> int:
        url = url.strip()
        if not url.startswith(("http://", "https://")):
            raise ServiceError("Enter a YouTube video or playlist URL")
        with self.session_factory() as session:
            mapping = session.get(Mapping, mapping_id)
            if mapping is None:
                raise ServiceError("Mapping not found")
            opts = settings_mod.resolve(session, mapping)
        try:
            entries = await asyncio.to_thread(self._fetch, url, opts, self.config.cookiefile())
        except DownloadFailed as exc:
            if exc.kind == ErrorKind.RATE_LIMIT:
                self.worker.enter_cooldown(exc.message)
            raise
        with self.session_factory() as session:
            mapping = session.get(Mapping, mapping_id)
            if mapping is None:
                raise ServiceError("Mapping not found")
            added = add_entries(session, mapping, entries, source="manual")
        if added:
            self.notifier.list_changed(mapping_id)
            self.worker.kick()
        return len(added)
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_sync.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add app/sync.py tests/conftest.py tests/test_sync.py
git commit -m "feat: additive playlist sync and manual add"
```

---

### Task 12: Download worker (queue, throttle, retries, cool-down, cancel)

**Files:**
- Create: `app/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: `ytdlp_client.download`, `DownloadFailed`, `Cancelled`, `VideoInfo` (Task 8); `organizer.place`, `organizer.apply` (Tasks 6–7); `settings.get_all`, `settings.resolve` (Task 2); `ActiveItem`, `ActivitySnapshot` (Task 10); `parse_artist_song` (Task 4); Notifier methods (Task 10)
- Produces:
  - `backoff_delay(attempts: int, schedule: list[int]) -> timedelta | None`
  - `cooldown_minutes(level: int, base: int, cap: int) -> int`
  - `class Worker(session_factory, config, notifier, *, download=ytdlp_client.download, now=utcnow, rng=None)` with:
    - attributes `paused: bool`, `cooldown_until: datetime|None`, `cooldown_reason: str|None`, `cooldown_level: int`, `next_download_at: datetime|None`, `restart_pending: bool`, `on_idle: Callable[[], None] | None`, `active: dict[int, ActiveItem]`
    - methods `kick()`, `pause()`, `resume()`, `cancel(video_id)`, `cancel_mapping(mapping_id)`, `enter_cooldown(reason)`, `is_blocked() -> bool`, `snapshot() -> ActivitySnapshot`, `publish_activity()`, `step() -> float`, `async run()`, `async stop()`, `async drain()`

- [ ] **Step 1: Write the failing tests**

`tests/test_worker.py`:
```python
import asyncio
import random
import threading
from datetime import timedelta
from pathlib import Path

from app.errors import ErrorKind
from app.models import MappingType, Video, VideoStatus
from app.worker import Worker, backoff_delay, cooldown_minutes
from app.ytdlp_client import Cancelled, DownloadFailed, VideoInfo


def make_info(yid, title=None, artist=None, track=None):
    return VideoInfo(youtube_id=yid, title=title or f"Title {yid}", channel="Chan", description="desc",
                     upload_date="20240101", duration=10, thumbnail_url=None, artist=artist, track=track)


def ok_download(info_factory=make_info):
    def download(yid, opts, staging, cookie, on_progress, is_cancelled):
        staging.mkdir(parents=True, exist_ok=True)
        on_progress({"stage": "downloading", "percent": 50.0, "speed": 1.0, "eta": 1, "detail": None})
        (staging / "media.mkv").write_text(f"video {yid}")
        (staging / "media.jpg").write_text("thumb")
        return info_factory(yid), staging / "media.mkv", [staging / "media.jpg"]
    return download


def failing(kind, message="boom"):
    def download(*args):
        raise DownloadFailed(kind, message)
    return download


def make_worker(session_factory, config, notifier, clock, download):
    return Worker(session_factory, config, notifier, download=download, now=clock, rng=random.Random(1))


def reload(session, video):
    session.expire_all()
    return session.get(Video, video.id)


def test_backoff_and_cooldown_math():
    assert backoff_delay(1, [10, 60]) == timedelta(minutes=10)
    assert backoff_delay(2, [10, 60]) == timedelta(minutes=60)
    assert backoff_delay(3, [10, 60]) is None
    assert [cooldown_minutes(n, 60, 300) for n in (1, 2, 3, 4)] == [60, 120, 240, 300]


async def test_downloads_pending_video_then_throttles(session, session_factory, config, notifier, clock,
                                                     make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "a")
    worker = make_worker(session_factory, config, notifier, clock, ok_download())
    assert worker.step() == 0.0
    await worker.drain()
    done = reload(session, video)
    assert done.status == VideoStatus.DONE
    assert (Path(mapping.folder) / done.file_path).read_text() == "video a"
    assert done.file_path == "Season 01/My Show S01E01 Title a.mkv"
    assert 30 <= (worker.next_download_at - clock.now).total_seconds() <= 90
    assert "progress" in notifier.names() and "video_changed" in notifier.names()
    assert not (config.staging_dir / str(video.id)).exists()

    add_video(mapping, "b")
    assert worker.step() > 0 and not worker.active
    clock.advance(seconds=91)
    assert worker.step() == 0.0
    await worker.drain()


async def test_transient_failure_backs_off(session, session_factory, config, notifier, clock, make_mapping, add_video):
    video = add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, failing(ErrorKind.TRANSIENT))
    worker.step()
    await worker.drain()
    failed = reload(session, video)
    assert failed.status == VideoStatus.FAILED and failed.attempts == 1 and failed.error == "boom"
    assert failed.next_retry_at == clock.now + timedelta(minutes=10)
    clock.advance(seconds=100)
    assert worker.step() > 0 and not worker.active
    clock.advance(minutes=10)
    assert worker.step() == 0.0
    await worker.drain()
    assert reload(session, video).attempts == 2


async def test_permanent_failure_marks_unavailable_and_renumbers(session, session_factory, config, notifier, clock,
                                                                 make_mapping, add_video):
    mapping = make_mapping()
    bad = add_video(mapping, "bad")
    (Path(mapping.folder) / "x.mkv").write_text("good")
    good = add_video(mapping, "good", status=VideoStatus.DONE, file_path="x.mkv")
    worker = make_worker(session_factory, config, notifier, clock, failing(ErrorKind.PERMANENT, "Private video"))
    worker.step()
    await worker.drain()
    assert reload(session, bad).status == VideoStatus.UNAVAILABLE
    assert reload(session, good).file_path == "Season 01/My Show S01E01 Title good.mkv"
    assert ("list_changed", (mapping.id,)) in notifier.calls


async def test_rate_limit_enters_doubling_cooldown(session, session_factory, config, notifier, clock,
                                                  make_mapping, add_video):
    video = add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, failing(ErrorKind.RATE_LIMIT, "HTTP Error 429"))
    worker.step()
    await worker.drain()
    assert reload(session, video).status == VideoStatus.PENDING
    assert worker.cooldown_until == clock.now + timedelta(minutes=60)
    assert worker.is_blocked() and worker.step() > 0 and not worker.active
    clock.advance(minutes=61)
    assert not worker.is_blocked()
    worker.step()
    await worker.drain()
    assert worker.cooldown_until == clock.now + timedelta(minutes=120)
    worker.resume()
    assert not worker.is_blocked()
    worker._download = ok_download()
    clock.advance(minutes=2)
    worker.step()
    await worker.drain()
    assert worker.cooldown_level == 0


async def test_pause_blocks_new_downloads(session_factory, config, notifier, clock, make_mapping, add_video):
    add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, ok_download())
    worker.pause()
    assert worker.step() == Worker.POLL_SECONDS and not worker.active
    assert worker.snapshot().queued == 1 and worker.snapshot().paused


async def test_restart_pending_runs_on_idle_when_nothing_active(session_factory, config, notifier, clock, make_mapping,
                                                               add_video):
    add_video(make_mapping(), "a")
    worker = make_worker(session_factory, config, notifier, clock, ok_download())
    calls = []
    worker.on_idle = lambda: calls.append(1)
    worker.restart_pending = True
    worker.step()
    assert calls == [1] and not worker.active


async def test_mapping_deleted_mid_download_leaves_no_files(session, session_factory, config, notifier, clock,
                                                            make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "a")
    started, release = threading.Event(), threading.Event()
    inner = ok_download()

    def download(*args):
        started.set()
        release.wait(5)
        return inner(*args)

    worker = make_worker(session_factory, config, notifier, clock, download)
    worker.step()
    await asyncio.to_thread(started.wait, 5)
    folder = Path(mapping.folder)
    session.delete(mapping)
    session.commit()
    release.set()
    await worker.drain()
    assert not [p for p in folder.rglob("*") if p.is_file()]
    assert not (config.staging_dir / str(video.id)).exists()
    assert not worker.active


async def test_cancel_stops_download(session, session_factory, config, notifier, clock, make_mapping, add_video):
    video = add_video(make_mapping(), "a")
    started = threading.Event()

    def download(yid, opts, staging, cookie, on_progress, is_cancelled):
        started.set()
        for _ in range(500):
            if is_cancelled():
                raise Cancelled()
            threading.Event().wait(0.01)
        raise AssertionError("never cancelled")

    worker = make_worker(session_factory, config, notifier, clock, download)
    worker.step()
    await asyncio.to_thread(started.wait, 5)
    worker.cancel(video.id)
    await worker.drain()
    assert not worker.active
    assert reload(session, video).status == VideoStatus.DOWNLOADING  # caller (delete) removes the row


async def test_music_metadata_reparsed_unless_user_edited(session, session_factory, config, notifier, clock,
                                                         make_mapping, add_video):
    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    auto = add_video(mapping, "auto", artist="Wrong", song="Wrong")
    edited = add_video(mapping, "edited", artist="Mine", song="Mine", user_edited=True)
    info = lambda yid: make_info(yid, title="X - Y", artist="Queen", track=f"Song {yid}")  # noqa: E731
    worker = make_worker(session_factory, config, notifier, clock, ok_download(info))
    for _ in range(2):
        worker.next_download_at = None
        worker.step()
        await worker.drain()
    assert (reload(session, auto).artist, reload(session, auto).song) == ("Queen", "Song auto")
    assert (reload(session, edited).artist, reload(session, edited).song) == ("Mine", "Mine")
    assert (Path(mapping.folder) / "Queen - Song auto.mkv").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_worker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.worker'`

- [ ] **Step 3: Implement `app/worker.py`**

```python
"""Download queue: one asyncio loop, yt-dlp in threads, all DB/FS work on the loop thread."""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_, select

from app import organizer
from app import settings as settings_mod
from app import ytdlp_client
from app.activity import ActiveItem, ActivitySnapshot
from app.errors import ErrorKind
from app.models import Mapping, MappingType, Video, VideoStatus
from app.mvparse import parse_artist_song
from app.timeutil import utcnow
from app.ytdlp_client import Cancelled, DownloadFailed, VideoInfo

log = logging.getLogger(__name__)


def backoff_delay(attempts: int, schedule: list[int]) -> timedelta | None:
    if attempts < 1 or attempts > len(schedule):
        return None
    return timedelta(minutes=schedule[attempts - 1])


def cooldown_minutes(level: int, base: int, cap: int) -> int:
    return min(base * 2 ** max(level - 1, 0), cap)


def _apply_info(video: Video, info: VideoInfo, mapping_type: str) -> None:
    video.yt_title = info.title
    video.channel = info.channel
    video.description = info.description
    video.upload_date = info.upload_date
    video.duration = info.duration
    video.thumbnail_url = info.thumbnail_url
    video.yt_artist = info.artist
    video.yt_track = info.track
    if mapping_type == MappingType.MUSIC_VIDEO and not video.user_edited:
        video.artist, video.song = parse_artist_song(info.title, info.channel, info.artist, info.track)


class Worker:
    POLL_SECONDS = 60.0
    PROGRESS_INTERVAL = 0.5

    def __init__(self, session_factory, config, notifier, *, download=ytdlp_client.download,
                 now: Callable[[], datetime] = utcnow, rng: random.Random | None = None) -> None:
        self.session_factory = session_factory
        self.config = config
        self.notifier = notifier
        self._download = download
        self._now = now
        self._rng = rng or random.Random()
        self.paused = False
        self.cooldown_until: datetime | None = None
        self.cooldown_reason: str | None = None
        self.cooldown_level = 0
        self.next_download_at: datetime | None = None
        self.restart_pending = False
        self.on_idle: Callable[[], None] | None = None
        self.active: dict[int, ActiveItem] = {}
        self._tasks: set[asyncio.Task] = set()
        self._cancelled: set[int] = set()
        self._wake = asyncio.Event()
        self._stopping = False

    # ----- control -------------------------------------------------------------------------
    def kick(self) -> None:
        self._wake.set()

    def pause(self) -> None:
        self.paused = True
        self.publish_activity()

    def resume(self) -> None:
        self.paused = False
        self.cooldown_until = None
        self.cooldown_reason = None
        self.kick()
        self.publish_activity()

    def cancel(self, video_id: int) -> None:
        if video_id in self.active:
            self._cancelled.add(video_id)

    def cancel_mapping(self, mapping_id: int) -> None:
        for item in self.active.values():
            if item.mapping_id == mapping_id:
                self._cancelled.add(item.video_id)

    def enter_cooldown(self, reason: str) -> None:
        with self.session_factory() as session:
            values = settings_mod.get_all(session)
        self.cooldown_level += 1
        minutes = cooldown_minutes(self.cooldown_level, values["cooldown_minutes"], values["cooldown_max_minutes"])
        self.cooldown_until = self._now() + timedelta(minutes=minutes)
        self.cooldown_reason = reason[:200]
        log.warning("YouTube rate limit hit (%s); pausing downloads for %d minutes", reason, minutes)
        self.publish_activity()

    def is_blocked(self) -> bool:
        if self.cooldown_until is not None and self._now() >= self.cooldown_until:
            self.cooldown_until = None
            self.cooldown_reason = None
        return self.paused or self.cooldown_until is not None

    # ----- state ---------------------------------------------------------------------------
    def _eligible(self):
        now = self._now()
        return and_(
            Mapping.enabled.is_(True),
            Video.id.not_in(list(self.active)),
            or_(
                Video.status == VideoStatus.PENDING,
                and_(Video.status == VideoStatus.FAILED, Video.next_retry_at.is_not(None), Video.next_retry_at <= now),
            ),
        )

    def snapshot(self) -> ActivitySnapshot:
        with self.session_factory() as session:
            queued = session.scalar(
                select(func.count(Video.id)).select_from(Video).join(Video.mapping).where(self._eligible())
            ) or 0
        return ActivitySnapshot(
            paused=self.paused, cooldown_until=self.cooldown_until, cooldown_reason=self.cooldown_reason,
            next_download_at=self.next_download_at, active=list(self.active.values()), queued=queued,
            restart_pending=self.restart_pending,
        )

    def publish_activity(self) -> None:
        self.notifier.activity(self.snapshot())

    # ----- loop ----------------------------------------------------------------------------
    async def run(self) -> None:
        self.publish_activity()
        while not self._stopping:
            self._wake.clear()
            try:
                delay = self.step()
            except Exception:
                log.exception("Worker step failed")
                delay = self.POLL_SECONDS
            if delay > 0:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except TimeoutError:
                    pass
            else:
                await asyncio.sleep(0)

    async def stop(self) -> None:
        self._stopping = True
        self._cancelled.update(self.active)
        self.kick()
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=15)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def step(self) -> float:
        """Start at most one download. Returns how long to wait before stepping again."""
        now = self._now()
        if self.restart_pending:
            if not self.active and self.on_idle is not None:
                self.on_idle()
            return self.POLL_SECONDS
        if self.is_blocked():
            if self.cooldown_until is not None:
                return max(1.0, min((self.cooldown_until - now).total_seconds(), self.POLL_SECONDS))
            return self.POLL_SECONDS
        if self.next_download_at is not None and now < self.next_download_at:
            return min((self.next_download_at - now).total_seconds(), self.POLL_SECONDS)
        with self.session_factory() as session:
            if len(self.active) >= settings_mod.get_all(session)["concurrency"]:
                return self.POLL_SECONDS
            video_id = session.scalar(
                select(Video.id).join(Video.mapping).where(self._eligible())
                .order_by(Video.mapping_id, Video.position).limit(1)
            )
            if video_id is None:
                return self.POLL_SECONDS
            self._start(session, video_id)
        return 0.0

    def _start(self, session, video_id: int) -> None:
        video = session.get(Video, video_id)
        mapping = video.mapping
        video.status = VideoStatus.DOWNLOADING
        video.error = None
        session.commit()
        opts = settings_mod.resolve(session, mapping)
        self.active[video_id] = ActiveItem(video_id=video_id, mapping_id=mapping.id,
                                           title=video.display_title, mapping_name=mapping.name)
        task = asyncio.create_task(self._process(video_id, video.youtube_id, opts))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log.info("Downloading %s (%s)", video.display_title, video.youtube_id)
        self.notifier.video_changed(video_id, mapping.id)
        self.publish_activity()

    async def _process(self, video_id: int, youtube_id: str, opts: dict) -> None:
        staging = self.config.staging_dir / str(video_id)
        shutil.rmtree(staging, ignore_errors=True)
        loop = asyncio.get_running_loop()
        last_sent = 0.0

        def on_progress(data: dict) -> None:
            nonlocal last_sent
            now = time.monotonic()
            if data.get("stage") == "downloading" and now - last_sent < self.PROGRESS_INTERVAL:
                return
            last_sent = now
            loop.call_soon_threadsafe(self._update_progress, video_id, data)

        try:
            info, media, extras = await asyncio.to_thread(
                self._download, youtube_id, opts, staging, self.config.cookiefile(),
                on_progress, lambda: video_id in self._cancelled,
            )
        except Cancelled:
            log.info("Download of %s cancelled", youtube_id)
        except DownloadFailed as exc:
            self._on_failure(video_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected error downloading %s", youtube_id)
            self._on_failure(video_id, DownloadFailed(ErrorKind.TRANSIENT, str(exc)))
        else:
            try:
                self._on_success(video_id, info, media, extras, opts)
            except Exception as exc:  # noqa: BLE001
                log.exception("Could not move %s into the library", youtube_id)
                self._on_failure(video_id, DownloadFailed(ErrorKind.TRANSIENT, f"Could not move file into library: {exc}"))
        finally:
            self._finish(video_id, staging)

    def _update_progress(self, video_id: int, data: dict) -> None:
        item = self.active.get(video_id)
        if item is None:
            return
        for key in ("stage", "percent", "speed", "eta", "detail"):
            setattr(item, key, data.get(key))
        self.notifier.progress(video_id, item)

    def _on_success(self, video_id: int, info: VideoInfo, media: Path, extras: list[Path], opts: dict) -> None:
        with self.session_factory() as session:
            video = session.get(Video, video_id)
            if video is None or video_id in self._cancelled:
                return
            mapping = video.mapping
            before = (video.display_title, video.artist, video.song)
            _apply_info(video, info, mapping.type)
            video.attempts = 0
            video.error = None
            video.next_retry_at = None
            organizer.place(session, self.config.media_root, mapping, video, media, extras, opts["max_filename_length"])
            names_changed = before != (video.display_title, video.artist, video.song)
            mapping_id = mapping.id
        self.cooldown_level = 0
        log.info("Downloaded %s", info.title)
        if names_changed:
            self.notifier.list_changed(mapping_id)
        self.notifier.video_changed(video_id, mapping_id)

    def _on_failure(self, video_id: int, err: DownloadFailed) -> None:
        with self.session_factory() as session:
            video = session.get(Video, video_id)
            if video is None:
                return
            mapping = video.mapping
            values = settings_mod.get_all(session)
            video.error = err.message
            if err.kind == ErrorKind.RATE_LIMIT:
                video.status = VideoStatus.PENDING
            elif err.kind == ErrorKind.PERMANENT:
                video.status = VideoStatus.UNAVAILABLE
                video.next_retry_at = None
            else:
                video.attempts += 1
                delay = backoff_delay(video.attempts, values["retry_backoff_minutes"])
                video.status = VideoStatus.FAILED
                video.next_retry_at = self._now() + delay if delay else None
            session.commit()
            log.warning("Download of %s failed (%s): %s", video.youtube_id, err.kind, err.message)
            if err.kind == ErrorKind.PERMANENT:
                organizer.apply(session, self.config.media_root, mapping, values["max_filename_length"])
            mapping_id = mapping.id
        if err.kind == ErrorKind.RATE_LIMIT:
            self.enter_cooldown(err.message)
        if err.kind == ErrorKind.PERMANENT:
            self.notifier.list_changed(mapping_id)
        self.notifier.video_changed(video_id, mapping_id)

    def _finish(self, video_id: int, staging: Path) -> None:
        self.active.pop(video_id, None)
        self._cancelled.discard(video_id)
        shutil.rmtree(staging, ignore_errors=True)
        with self.session_factory() as session:
            values = settings_mod.get_all(session)
        delay = self._rng.uniform(values["min_delay_seconds"], values["max_delay_seconds"])
        self.next_download_at = self._now() + timedelta(seconds=delay)
        self.publish_activity()
        self.kick()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_worker.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add app/worker.py tests/test_worker.py
git commit -m "feat: download worker with throttling, retries and rate-limit cool-down"
```

---

### Task 13: yt-dlp updater and scheduler

**Files:**
- Create: `app/updater.py`, `app/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `Worker.is_blocked()`, `Worker.restart_pending`, `Worker.kick()` (Task 12); `Syncer.sync(mapping_id)` (Task 11); `settings.get_all` (Task 2)
- Produces:
  - `updater.running_version() -> str`, `updater.installed_version() -> str`, `async updater.pip_upgrade() -> tuple[int, str]`
  - `class Updater(worker, *, upgrade=pip_upgrade, installed=installed_version, running=running_version)` with `async check() -> str`, attributes `last_checked_at: datetime|None`, `last_result: str|None`
  - `class Scheduler(session_factory, worker, syncer, updater, *, now=utcnow, rng=None, sleep=asyncio.sleep)` with `due_mapping_ids(session) -> list[int]`, `async tick()`, `async run()`

- [ ] **Step 1: Write the failing tests**

`tests/test_scheduler.py`:
```python
import random
from datetime import timedelta

from app import settings as settings_mod
from app.scheduler import Scheduler
from app.updater import Updater

URL = "https://www.youtube.com/playlist?list=PL"


class FakeSyncer:
    def __init__(self):
        self.synced = []

    async def sync(self, mapping_id):
        self.synced.append(mapping_id)
        return 0


class FakeUpdater:
    def __init__(self):
        self.checks = 0

    async def check(self):
        self.checks += 1
        return "ok"


def make_scheduler(session_factory, fake_worker, clock, sleeps):
    async def sleep(seconds):
        sleeps.append(seconds)

    return Scheduler(session_factory, fake_worker, FakeSyncer(), FakeUpdater(), now=clock,
                     rng=random.Random(0), sleep=sleep)


def test_due_mapping_ids(session, session_factory, fake_worker, clock, make_mapping):
    never = make_mapping("Never", playlist_url=URL)
    recent = make_mapping("Recent", playlist_url=URL, last_synced_at=clock.now - timedelta(minutes=10))
    old = make_mapping("Old", playlist_url=URL, last_synced_at=clock.now - timedelta(minutes=400))
    custom = make_mapping("Custom", playlist_url=URL, sync_interval_minutes=5,
                          last_synced_at=clock.now - timedelta(minutes=6))
    make_mapping("Manual")
    make_mapping("Disabled", playlist_url=URL, enabled=False)
    scheduler = make_scheduler(session_factory, fake_worker, clock, [])
    assert scheduler.due_mapping_ids(session) == [never.id, old.id, custom.id]
    assert recent.id not in scheduler.due_mapping_ids(session)


async def test_tick_syncs_due_mappings_with_jitter_between(session, session_factory, fake_worker, clock, make_mapping):
    settings_mod.set_many(session, {"ytdlp_auto_update": False})
    a = make_mapping("A", playlist_url=URL)
    b = make_mapping("B", playlist_url=URL)
    sleeps = []
    scheduler = make_scheduler(session_factory, fake_worker, clock, sleeps)
    await scheduler.tick()
    assert scheduler.syncer.synced == [a.id, b.id]
    assert len(sleeps) == 1 and 0 <= sleeps[0] <= 300


async def test_tick_does_nothing_while_blocked(session_factory, fake_worker, clock, make_mapping):
    make_mapping("A", playlist_url=URL)
    fake_worker.blocked = True
    scheduler = make_scheduler(session_factory, fake_worker, clock, [])
    await scheduler.tick()
    assert scheduler.syncer.synced == []


async def test_update_check_runs_at_start_and_daily(session_factory, fake_worker, clock):
    scheduler = make_scheduler(session_factory, fake_worker, clock, [])
    await scheduler.tick()
    await scheduler.tick()
    assert scheduler.updater.checks == 1
    clock.advance(hours=24)
    await scheduler.tick()
    assert scheduler.updater.checks == 2


async def test_updater_requests_restart_when_new_version_installed(fake_worker):
    async def upgrade():
        return 0, "Successfully installed yt-dlp-2026.1.1"

    updater = Updater(fake_worker, upgrade=upgrade, installed=lambda: "2026.1.1", running=lambda: "2025.12.1")
    result = await updater.check()
    assert fake_worker.restart_pending is True and "2026.1.1" in result and fake_worker.kicks == 1


async def test_updater_up_to_date_and_failure(fake_worker):
    async def ok():
        return 0, ""

    async def broken():
        return 1, "line\nERROR: no network"

    assert "Up to date" in await Updater(fake_worker, upgrade=ok, installed=lambda: "1", running=lambda: "1").check()
    result = await Updater(fake_worker, upgrade=broken, installed=lambda: "1", running=lambda: "1").check()
    assert result == "Update failed: ERROR: no network"
    assert fake_worker.restart_pending is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.scheduler'`

- [ ] **Step 3: Implement `app/updater.py`**

```python
from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import logging
import sys
from datetime import datetime

from app.timeutil import utcnow

log = logging.getLogger(__name__)


def running_version() -> str:
    from yt_dlp.version import __version__

    return __version__


def installed_version() -> str:
    importlib.invalidate_caches()
    return importlib.metadata.version("yt-dlp")


async def pip_upgrade() -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
        "--upgrade", "yt-dlp[default]",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await proc.communicate()
    return proc.returncode or 0, output.decode(errors="replace")


class Updater:
    def __init__(self, worker, *, upgrade=pip_upgrade, installed=installed_version, running=running_version) -> None:
        self.worker = worker
        self._upgrade = upgrade
        self._installed = installed
        self._running = running
        self._lock = asyncio.Lock()
        self.last_checked_at: datetime | None = None
        self.last_result: str | None = None

    async def check(self) -> str:
        async with self._lock:
            log.info("Checking for yt-dlp updates")
            code, output = await self._upgrade()
            self.last_checked_at = utcnow()
            if code != 0:
                lines = [line for line in output.strip().splitlines() if line.strip()]
                self.last_result = f"Update failed: {lines[-1] if lines else f'pip exited with {code}'}"
                log.error(self.last_result)
                return self.last_result
            installed, running = self._installed(), self._running()
            if installed != running:
                self.last_result = f"Installed yt-dlp {installed}; restarting when idle"
                self.worker.restart_pending = True
                self.worker.kick()
            else:
                self.last_result = f"Up to date ({running})"
            log.info(self.last_result)
            return self.last_result
```

- [ ] **Step 4: Implement `app/scheduler.py`**

```python
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import select

from app import settings as settings_mod
from app.models import Mapping
from app.timeutil import utcnow

log = logging.getLogger(__name__)


class Scheduler:
    TICK_SECONDS = 30
    UPDATE_INTERVAL = timedelta(hours=24)

    def __init__(self, session_factory, worker, syncer, updater, *, now=utcnow,
                 rng: random.Random | None = None, sleep=asyncio.sleep) -> None:
        self.session_factory = session_factory
        self.worker = worker
        self.syncer = syncer
        self.updater = updater
        self._now = now
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._last_update_check: datetime | None = None

    def due_mapping_ids(self, session) -> list[int]:
        default_interval = settings_mod.get_all(session)["sync_interval_minutes"]
        now = self._now()
        due = []
        mappings = session.scalars(
            select(Mapping).where(Mapping.enabled.is_(True), Mapping.playlist_url.is_not(None)).order_by(Mapping.id)
        )
        for mapping in mappings:
            interval = timedelta(minutes=mapping.sync_interval_minutes or default_interval)
            if mapping.last_synced_at is None or mapping.last_synced_at + interval <= now:
                due.append(mapping.id)
        return due

    async def tick(self) -> None:
        with self.session_factory() as session:
            values = settings_mod.get_all(session)
        now = self._now()
        if values["ytdlp_auto_update"] and (
            self._last_update_check is None or now - self._last_update_check >= self.UPDATE_INTERVAL
        ):
            self._last_update_check = now
            try:
                await self.updater.check()
            except Exception:
                log.exception("yt-dlp update check failed")
        if self.worker.is_blocked():
            return
        with self.session_factory() as session:
            due = self.due_mapping_ids(session)
        for index, mapping_id in enumerate(due):
            if self.worker.is_blocked():
                break
            if index > 0 and values["sync_jitter_seconds"]:
                await self._sleep(self._rng.uniform(0, values["sync_jitter_seconds"]))
            await self.syncer.sync(mapping_id)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Scheduler tick failed")
            await self._sleep(self.TICK_SECONDS)
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add app/updater.py app/scheduler.py tests/test_scheduler.py
git commit -m "feat: staggered sync scheduler and yt-dlp self-updater"
```

---
### Task 14: Web app skeleton: app factory, auth, layout, SSE endpoint, dashboard, logs

**Files:**
- Create: `app/main.py`, `app/__main__.py`, `app/web/__init__.py` (empty), `app/web/deps.py`, `app/web/dashboard.py`, `app/web/sse.py`
- Create: `app/templates/base.html`, `app/templates/dashboard.html`, `app/templates/logs.html`, `app/templates/partials/mapping_card.html`
- Create: `app/static/app.css`, `app/static/app.js`, `app/static/vendor/{htmx.min.js,sse.js,Sortable.min.js}`
- Modify: `tests/conftest.py` (add `app` and `client` fixtures)
- Test: `tests/test_web_dashboard.py`

**Interfaces:**
- Consumes: everything from Tasks 1–13
- Produces:
  - `main.create_app(config: AppConfig | None = None, *, start_background: bool = True) -> FastAPI`. `app.state` holds `config, session_factory, bus, notifier, worker, syncer, updater, scheduler, log_buffer, exit_code, server, background`.
  - `main.request_restart(app)`, `main.RESTART_EXIT_CODE = 3`, `main.BasicAuthMiddleware`
  - `web.deps`: `get_session`, `SessionDep`, `render(request, name, context=None, status_code=200)`, `is_htmx(request)`, `done(request, url)`, `get_or_404(session, model, id)`, `spawn(app, coro) -> asyncio.Task`
  - `web.sse.event_stream(bus, queue, is_disconnected) -> AsyncIterator[str]`
  - Routes: `GET /`, `GET /healthz`, `GET /mappings/{id}/card`, `GET /activity`, `POST /queue/pause`, `POST /queue/resume`, `GET /logs`, `GET /events`
  - Front-end conventions every later template relies on:
    - `<body hx-ext="sse" sse-connect="/events">`
    - trigger elements use `hx-trigger="sse:<event>"`
    - drag lists use `data-sortable="<post url>"` and swap `#video-list`
    - countdowns use `data-countdown="<iso utc>"`
    - folder pick buttons use `data-pick-folder="<rel path>"`
    - show-only fields use `data-show-only`
    - htmx swaps 422 responses (validation errors)
    - other 4xx/5xx responses on `hx-swap="none"` actions appear in `#toast`

- [ ] **Step 1: Vendor the front-end libraries**

```bash
mkdir -p app/static/vendor
curl -fsSL -o app/static/vendor/htmx.min.js https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js
curl -fsSL -o app/static/vendor/sse.js https://unpkg.com/htmx-ext-sse@2.2.2/sse.js
curl -fsSL -o app/static/vendor/Sortable.min.js https://unpkg.com/sortablejs@1.15.6/Sortable.min.js
head -c 200 app/static/vendor/htmx.min.js; echo; head -c 200 app/static/vendor/sse.js; echo; head -c 200 app/static/vendor/Sortable.min.js
```
Expected: three non-empty JavaScript files. The heads show JS source, not HTML error pages.

- [ ] **Step 2: Add web fixtures to `tests/conftest.py`**

Append:
```python
@pytest.fixture
def app(config):
    from app.main import create_app

    return create_app(config, start_background=False)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


def wait_until(predicate, timeout: float = 2.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")
```

- [ ] **Step 3: Write the failing tests**

`tests/test_web_dashboard.py`:
```python
import asyncio
import base64
import logging
from dataclasses import replace

from fastapi.testclient import TestClient

from app.events import EventBus
from app.main import create_app
from app.models import VideoStatus
from app.web.sse import event_stream

HX = {"HX-Request": "true"}


def test_dashboard_empty(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "No mappings yet" in response.text
    assert 'sse-connect="/events"' in response.text


def test_dashboard_lists_mapping_with_counts(client, make_mapping, add_video):
    mapping = make_mapping("Veritasium", playlist_url="https://www.youtube.com/playlist?list=PL")
    add_video(mapping, "a", status=VideoStatus.DONE)
    add_video(mapping, "b")
    page = client.get("/").text
    assert "Veritasium" in page and "1 done" in page and "1 queued" in page and "Sync now" in page
    card = client.get(f"/mappings/{mapping.id}/card")
    assert card.status_code == 200 and f'hx-trigger="sse:mapping-{mapping.id}"' in card.text
    assert client.get("/mappings/999/card").status_code == 404


def test_healthz_and_static(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/static/app.js").status_code == 200


def test_activity_partial(client):
    response = client.get("/activity")
    assert response.status_code == 200 and "waiting" in response.text


def test_pause_and_resume(client, app):
    assert client.post("/queue/pause", headers=HX).status_code == 204
    assert app.state.worker.paused
    response = client.post("/queue/resume", follow_redirects=False)
    assert response.status_code == 303 and not app.state.worker.paused


def test_logs_page_shows_buffered_lines(client):
    logging.getLogger("test").warning("hello-log-line")
    assert "hello-log-line" in client.get("/logs").text


def test_basic_auth(config):
    app = create_app(replace(config, auth_user="u", auth_password="p"), start_background=False)
    with TestClient(app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/healthz").status_code == 200
        good = base64.b64encode(b"u:p").decode()
        bad = base64.b64encode(b"u:x").decode()
        assert client.get("/", headers={"Authorization": f"Basic {good}"}).status_code == 200
        assert client.get("/", headers={"Authorization": f"Basic {bad}"}).status_code == 401


async def test_event_stream_formats_and_unsubscribes():
    bus = EventBus()
    bus.bind(asyncio.get_running_loop())
    queue = bus.subscribe()

    async def connected():
        return False

    stream = event_stream(bus, queue, connected)
    assert await stream.__anext__() == "retry: 3000\n\n"
    bus.publish("x", "hi")
    assert await stream.__anext__() == "event: x\ndata: hi\n\n"
    await stream.aclose()
    assert queue not in bus._subscribers
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_dashboard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.main'`

- [ ] **Step 5: Implement `app/web/deps.py` and `app/web/sse.py`**

`app/web/deps.py`:
```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.templating import templates


async def get_session(request: Request) -> AsyncIterator[Session]:
    # async dependency: the session lives on the event-loop thread, like all other DB work
    with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def render(request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200) -> Response:
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def done(request: Request, url: str) -> Response:
    """Response for a fire-and-forget action: 204 for htmx, a redirect for plain forms."""
    if is_htmx(request):
        return Response(status_code=204)
    return RedirectResponse(url, status_code=303)


def get_or_404(session: Session, model: type, object_id: int):
    obj = session.get(model, object_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Not found")
    return obj


def spawn(app: FastAPI, coro: Coroutine) -> asyncio.Task:
    task = asyncio.create_task(coro)
    app.state.background.add(task)
    task.add_done_callback(app.state.background.discard)
    return task
```

`app/web/sse.py`:
```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.events import Event, EventBus, format_sse

router = APIRouter()
KEEPALIVE_SECONDS = 15


async def event_stream(bus: EventBus, queue: asyncio.Queue[Event],
                       is_disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[str]:
    try:
        yield "retry: 3000\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                if await is_disconnected():
                    break
                yield ": keepalive\n\n"
                continue
            yield format_sse(event)
    finally:
        bus.unsubscribe(queue)


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    bus: EventBus = request.app.state.bus
    queue = bus.subscribe()
    return StreamingResponse(
        event_stream(bus, queue, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

- [ ] **Step 6: Implement `app/web/dashboard.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from app import services
from app.models import Mapping
from app.web.deps import SessionDep, done, get_or_404, render

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@router.get("/")
async def index(request: Request, session: SessionDep):
    mappings = session.scalars(select(Mapping).order_by(Mapping.name)).all()
    return render(request, "dashboard.html", {
        "mappings": mappings, "stats": services.mapping_stats(session), "empty": services.EMPTY_STATS,
        "syncing": request.app.state.syncer.running,
    })


@router.get("/mappings/{mapping_id}/card")
async def mapping_card(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    return render(request, "partials/mapping_card.html", {
        "m": mapping, "stats": services.mapping_stats(session), "empty": services.EMPTY_STATS,
        "syncing": request.app.state.syncer.running,
    })


@router.get("/activity")
async def activity(request: Request):
    return render(request, "partials/activity.html", {"a": request.app.state.worker.snapshot()})


@router.post("/queue/pause")
async def pause(request: Request):
    request.app.state.worker.pause()
    return done(request, "/")


@router.post("/queue/resume")
async def resume(request: Request):
    request.app.state.worker.resume()
    return done(request, "/")


@router.get("/logs")
async def logs(request: Request):
    return render(request, "logs.html", {"lines": list(request.app.state.log_buffer.lines)})
```

- [ ] **Step 7: Implement `app/main.py` and `app/__main__.py`**

`app/main.py`:
```python
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import PlainTextResponse

from app import organizer
from app import settings as settings_mod
from app.config import AppConfig, load_config
from app.db import init_db, make_engine, make_session_factory
from app.events import EventBus
from app.logbuffer import LogBuffer, setup_logging
from app.notify import Notifier
from app.scheduler import Scheduler
from app.sync import Syncer
from app.templating import templates
from app.updater import Updater
from app.web import dashboard, sse
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


def request_restart(app: FastAPI) -> None:
    app.state.exit_code = RESTART_EXIT_CODE
    server = app.state.server
    if server is not None:
        server.should_exit = True


def create_app(config: AppConfig | None = None, *, start_background: bool = True) -> FastAPI:
    config = config or load_config()
    config.config_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(config.db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    bus = EventBus()
    notifier = Notifier(bus, templates.env)
    log_buffer = LogBuffer()
    setup_logging(config, log_buffer)
    log_buffer.listener = notifier.log_line
    worker = Worker(session_factory, config, notifier)
    syncer = Syncer(session_factory, config, worker, notifier)
    updater = Updater(worker)
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
            tasks = [asyncio.create_task(worker.run()), asyncio.create_task(scheduler.run())]
        log.info("yt-jellyfin-sync started (media=%s, config=%s)", config.media_root, config.config_dir)
        try:
            yield
        finally:
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
    app.state.scheduler = scheduler
    app.state.log_buffer = log_buffer
    app.state.exit_code = 0
    app.state.server = None
    app.state.background = set()
    if config.auth_user and config.auth_password:
        app.add_middleware(BasicAuthMiddleware, username=config.auth_user, password=config.auth_password)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    for module in (dashboard, sse):
        app.include_router(module.router)
    return app
```

`app/__main__.py`:
```python
from __future__ import annotations

import sys

import uvicorn

from app.config import load_config
from app.main import create_app


def main() -> None:
    config = load_config()
    app = create_app(config)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=config.port, log_config=None))
    app.state.server = server
    server.run()
    sys.exit(app.state.exit_code)


if __name__ == "__main__":
    main()
```

- [ ] **Step 8: Write the layout templates**

`app/templates/base.html`:
```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}YT → Jellyfin{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="/static/vendor/htmx.min.js"></script>
  <script src="/static/vendor/sse.js"></script>
  <script src="/static/vendor/Sortable.min.js"></script>
  <script src="/static/app.js" defer></script>
</head>
<body hx-ext="sse" sse-connect="/events">
  <header class="topbar">
    <a class="brand" href="/">YT → Jellyfin</a>
    <nav>
      <a href="/">Mappings</a>
      <a href="/mappings/new">New mapping</a>
      <a href="/settings">Settings</a>
      <a href="/logs">Logs</a>
    </nav>
  </header>
  <section id="activity" sse-swap="activity" hx-get="/activity" hx-trigger="load"></section>
  <main class="container">
    {% block content %}{% endblock %}
  </main>
  <div id="toast" class="toast" hidden></div>
</body>
</html>
```

`app/templates/dashboard.html`:
```html
{% extends "base.html" %}
{% block content %}
<div class="page-head">
  <h1>Mappings</h1>
  <a class="btn" href="/mappings/new">New mapping</a>
</div>
{% if not mappings %}
<div class="empty">No mappings yet. <a href="/mappings/new">Create your first one</a>: map a YouTube playlist, or a hand-picked list, to a Jellyfin folder.</div>
{% endif %}
<div class="cards">
  {% for m in mappings %}{% include "partials/mapping_card.html" %}{% endfor %}
</div>
{% endblock %}
```

`app/templates/partials/mapping_card.html`:
```html
{% set s = stats.get(m.id, empty) %}
<article class="card" id="mapping-card-{{ m.id }}" hx-get="/mappings/{{ m.id }}/card" hx-trigger="sse:mapping-{{ m.id }}" hx-swap="outerHTML">
  <header class="card-head">
    <a class="card-title" href="/mappings/{{ m.id }}">{{ m.name }}</a>
    <span class="tag">{{ "Show" if m.type == "show" else "Music videos" }}</span>
    {% if not m.enabled %}<span class="tag">disabled</span>{% endif %}
  </header>
  <div class="card-folder" title="{{ m.folder }}">{{ m.folder }}</div>
  <div class="counts">
    <span class="count done">{{ s.done }} done</span>
    <span class="count">{{ s.pending + s.downloading }} queued</span>
    {% if s.failed %}<span class="count failed">{{ s.failed }} failed</span>{% endif %}
    {% if s.unavailable %}<span class="count unavailable">{{ s.unavailable }} unavailable</span>{% endif %}
  </div>
  <footer class="card-foot">
    {% if m.playlist_url %}
      <span class="muted">Synced {{ m.last_synced_at|localtime }}</span>
      <span class="sync-status muted" sse-swap="sync-{{ m.id }}">{% if m.id in syncing %}Checking playlist…{% elif m.last_sync_error %}<span class="error-text">Last sync failed</span>{% endif %}</span>
      <button class="btn small push-right" hx-post="/mappings/{{ m.id }}/sync" hx-swap="none">Sync now</button>
    {% else %}
      <span class="muted">Manual list</span>
    {% endif %}
  </footer>
</article>
```

`app/templates/logs.html`:
```html
{% extends "base.html" %}
{% block title %}Logs · YT → Jellyfin{% endblock %}
{% block content %}
<div class="page-head">
  <h1>Live log</h1>
  <label class="check muted"><input type="checkbox" id="log-follow" checked> Follow</label>
</div>
<div id="log" class="log" sse-swap="log" hx-swap="beforeend">{% for line in lines %}<div class="log-line">{{ line }}</div>{% endfor %}</div>
{% endblock %}
```

- [ ] **Step 9: Write `app/static/app.js` and `app/static/app.css`**

`app/static/app.js`:
```js
(() => {
  "use strict";

  // Swap 422 validation responses (partials carrying an error banner); treat other 4xx/5xx as errors.
  htmx.config.responseHandling = [
    { code: "204", swap: false },
    { code: "[23]..", swap: true },
    { code: "422", swap: true },
    { code: "[45]..", swap: false, error: true },
    { code: "...", swap: true },
  ];

  function remaining(ms) {
    if (ms <= 0) return "now";
    const s = Math.ceil(ms / 1000);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m ${sec}s`;
    return `${sec}s`;
  }

  function tickCountdowns() {
    document.querySelectorAll("[data-countdown]").forEach((el) => {
      const target = Date.parse(el.dataset.countdown);
      if (!Number.isNaN(target)) el.textContent = remaining(target - Date.now());
    });
  }

  function initSortables() {
    document.querySelectorAll("[data-sortable]").forEach((list) => {
      if (list._sortable) return;
      list._sortable = Sortable.create(list, {
        handle: ".drag",
        draggable: "[data-video-id]",
        animation: 150,
        onEnd() {
          const order = [...list.querySelectorAll("[data-video-id]")].map((el) => el.dataset.videoId).join(",");
          htmx.ajax("POST", list.dataset.sortable, { target: "#video-list", swap: "outerHTML", values: { order } });
        },
      });
    });
  }

  function followLog() {
    const log = document.getElementById("log");
    if (!log) return;
    while (log.children.length > 1000) log.firstElementChild.remove();
    const follow = document.getElementById("log-follow");
    if (!follow || follow.checked) log.scrollTop = log.scrollHeight;
  }

  function toggleTypeFields() {
    const type = document.querySelector("[name=type]");
    if (!type) return;
    document.querySelectorAll("[data-show-only]").forEach((el) => { el.hidden = type.value !== "show"; });
  }

  function showToast(text) {
    const toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = text.slice(0, 300);
    toast.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toast.hidden = true; }, 6000);
  }

  function refresh() { tickCountdowns(); initSortables(); followLog(); }

  document.addEventListener("DOMContentLoaded", () => { refresh(); toggleTypeFields(); });
  document.addEventListener("htmx:afterSettle", refresh);
  document.addEventListener("change", (e) => { if (e.target.matches("[name=type]")) toggleTypeFields(); });
  document.addEventListener("htmx:responseError", (e) => {
    const xhr = e.detail.xhr;
    showToast(xhr.responseText && xhr.responseText.length < 300 ? xhr.responseText : `Request failed (${xhr.status})`);
  });
  document.addEventListener("click", (e) => {
    const pick = e.target.closest("[data-pick-folder]");
    if (!pick) return;
    document.getElementById("folder").value = pick.dataset.pickFolder;
    document.getElementById("folder-picker").innerHTML = "";
  });
  setInterval(tickCountdowns, 1000);
})();
```

`app/static/app.css`:
```css
:root {
  --bg: #f5f6f8; --panel: #fff; --text: #1c2230; --muted: #6b7385; --border: #e2e5eb;
  --accent: #5b4de0; --accent-soft: #ece9ff; --ok: #1f8a4c; --ok-soft: #e3f5ea;
  --warn: #a3620a; --warn-soft: #fdf1dc; --err: #c93a3a; --err-soft: #fde6e6;
  --radius: 10px; --shadow: 0 1px 2px rgba(16, 24, 40, .06); color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --panel: #181b21; --text: #e7e9ee; --muted: #98a0b0; --border: #2a2e37;
    --accent: #8f84ff; --accent-soft: #25223f; --ok: #4cc38a; --ok-soft: #14291e;
    --warn: #f0b35a; --warn-soft: #2e2412; --err: #f07474; --err-soft: #341a1a; color-scheme: dark;
  }
}
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
[hidden] { display: none !important; }
.topbar { display: flex; align-items: center; gap: 24px; padding: 12px 24px; background: var(--panel); border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 10; }
.brand { font-weight: 700; color: var(--text); }
.topbar nav { display: flex; gap: 16px; }
.topbar nav a { color: var(--muted); }
.topbar nav a:hover { color: var(--text); text-decoration: none; }
.container { max-width: 1100px; margin: 0 auto; padding: 24px; }
#activity { max-width: 1100px; margin: 16px auto 0; padding: 0 24px; }
.activity-bar { display: flex; align-items: center; gap: 8px; padding: 8px 12px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.activity-label { font-weight: 600; }
.activity-item { margin-top: 8px; padding: 8px 12px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.activity-title { font-size: .9em; margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.push-right { margin-left: auto; }
.muted { color: var(--muted); }
.page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.page-head h1 { margin: 0 0 4px; font-size: 1.5rem; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; margin-bottom: 16px; box-shadow: var(--shadow); }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px; border-radius: 8px; border: 1px solid var(--accent); background: var(--accent); color: #fff; font: inherit; cursor: pointer; white-space: nowrap; }
.btn:hover { filter: brightness(1.08); text-decoration: none; }
.btn.ghost { background: transparent; color: var(--text); border-color: var(--border); }
.btn.danger { background: transparent; color: var(--err); border-color: var(--err); }
.btn.small { padding: 3px 10px; font-size: .85em; }
button.link { background: none; border: none; color: var(--accent); cursor: pointer; font: inherit; padding: 0; }
.banner { padding: 10px 14px; border-radius: var(--radius); margin-bottom: 12px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.banner.info { background: var(--accent-soft); }
.banner.warn { background: var(--warn-soft); color: var(--warn); }
.banner.error { background: var(--err-soft); color: var(--err); }
.empty { padding: 32px; text-align: center; color: var(--muted); border: 1px dashed var(--border); border-radius: var(--radius); }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; display: flex; flex-direction: column; gap: 8px; box-shadow: var(--shadow); }
.card-head { display: flex; align-items: center; gap: 8px; }
.card-title { font-weight: 600; font-size: 1.1em; color: var(--text); }
.card-folder { font-family: ui-monospace, monospace; font-size: .8em; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card-foot { display: flex; align-items: center; gap: 8px; font-size: .85em; flex-wrap: wrap; }
.tag { font-size: .75em; padding: 1px 8px; border-radius: 999px; background: var(--accent-soft); color: var(--accent); }
.counts { display: flex; gap: 8px; flex-wrap: wrap; }
.count, .badge { font-size: .8em; padding: 2px 8px; border-radius: 999px; background: var(--bg); }
.count.done, .badge.done { background: var(--ok-soft); color: var(--ok); }
.count.failed, .badge.failed { background: var(--err-soft); color: var(--err); }
.count.unavailable, .badge.unavailable { background: var(--warn-soft); color: var(--warn); }
.badge.pending { color: var(--muted); }
.error-text { color: var(--err); font-size: .85em; }
.progress { height: 6px; border-radius: 3px; background: var(--border); overflow: hidden; position: relative; }
.progress .bar { height: 100%; background: var(--accent); transition: width .4s ease; }
.progress.indeterminate .bar { width: 30%; position: absolute; animation: indeterminate 1.2s ease-in-out infinite; }
@keyframes indeterminate { 0% { left: -30%; } 100% { left: 100%; } }
.progress-text { font-size: .8em; color: var(--muted); }
input, select { font: inherit; color: inherit; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 7px 10px; width: 100%; }
input[type=checkbox] { width: auto; }
.form { display: flex; flex-direction: column; gap: 14px; max-width: 760px; }
.form label, .settings label { display: flex; flex-direction: column; gap: 4px; font-weight: 500; }
.form label.check, .settings label.check, label.check { flex-direction: row; align-items: center; gap: 8px; }
label small, .hint { font-weight: 400; color: var(--muted); font-size: .85em; }
.row { display: flex; gap: 8px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; margin-top: 12px; }
.actions { display: flex; gap: 8px; align-items: center; }
fieldset { border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; margin: 0 0 16px; background: var(--panel); }
legend { font-weight: 600; padding: 0 6px; }
.add-video { display: flex; gap: 8px; align-items: center; }
.videos { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.video { display: grid; grid-template-columns: 20px 120px minmax(0, 1fr) 210px auto; gap: 12px; align-items: center; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); padding: 8px 12px; }
.video.sortable-ghost { opacity: .4; }
.drag { cursor: grab; color: var(--muted); user-select: none; }
.thumb { width: 120px; aspect-ratio: 16 / 9; object-fit: cover; border-radius: 6px; background: var(--bg); }
.meta .title { font-weight: 500; }
.code { font-family: ui-monospace, monospace; font-size: .85em; color: var(--accent); margin-right: 4px; }
.filename { font-family: ui-monospace, monospace; font-size: .75em; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.yt-link { font-size: .75em; color: var(--muted); }
.row-actions { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
.season-header { font-weight: 700; margin: 12px 0 2px; color: var(--muted); text-transform: uppercase; font-size: .8em; letter-spacing: .06em; }
.video.editing { grid-template-columns: 1fr; }
.edit-form { display: flex; gap: 8px; align-items: flex-end; flex-wrap: wrap; }
.edit-form label { display: flex; flex-direction: column; flex: 1; min-width: 200px; }
.picker { border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; background: var(--bg); }
.picker-head { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.picker-list { list-style: none; margin: 0 0 8px; padding: 0; max-height: 240px; overflow: auto; }
.picker-list li { display: flex; justify-content: space-between; padding: 3px 0; }
.inline { display: inline-flex; gap: 6px; align-items: center; }
.ignored { list-style: none; padding: 0; }
.ignored li { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; }
.log { font-family: ui-monospace, monospace; font-size: .8em; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px; height: 70vh; overflow: auto; white-space: pre-wrap; }
.toast { position: fixed; bottom: 20px; right: 20px; max-width: 420px; background: var(--err); color: #fff; padding: 10px 14px; border-radius: var(--radius); box-shadow: 0 4px 12px rgba(0, 0, 0, .2); }
.danger-zone { border-color: var(--err); }
.danger-zone h2 { margin-top: 0; font-size: 1.1rem; color: var(--err); }
.htmx-indicator { display: none; }
.htmx-request .htmx-indicator, .htmx-request.htmx-indicator { display: inline; }
@media (max-width: 760px) {
  .topbar { flex-wrap: wrap; gap: 8px 16px; padding: 12px 16px; }
  .container, #activity { padding-left: 16px; padding-right: 16px; }
  .video { grid-template-columns: 20px minmax(0, 1fr); }
  .video .thumb { display: none; }
  .video .status, .video .row-actions { grid-column: 1 / -1; }
}
```

- [ ] **Step 10: Run tests**

Run: `.venv/bin/pytest tests/test_web_dashboard.py -v`
Expected: all PASS

- [ ] **Step 11: Commit**

```bash
git add app/main.py app/__main__.py app/web app/templates app/static tests/conftest.py tests/test_web_dashboard.py
git commit -m "feat: web app skeleton with realtime layout, dashboard, logs and auth"
```

---

### Task 15: Mapping create/edit/delete pages and folder picker

**Files:**
- Create: `app/web/mappings.py`, `app/templates/mapping_form.html`, `app/templates/partials/folder_picker.html`
- Modify: `app/main.py` (include the `mappings` router)
- Test: `tests/test_web_mappings.py`

**Interfaces:**
- Consumes: `services.create_mapping/update_mapping/delete_mapping`, `MappingInput`, `ServiceError` (Task 9); `settings.CHOICES/OVERRIDABLE/get_all` (Task 2); `naming.sanitize` (Task 3); `deps.*` (Task 14); `app.state.syncer.sync`, `app.state.worker.cancel_mapping`, `app.state.worker.cooldown_until`
- Produces routes:
  - `GET /mappings/new`, `POST /mappings`
  - `GET /mappings/{id}/edit`, `POST /mappings/{id}/edit`, `POST /mappings/{id}/delete`
  - `POST /mappings/{id}/sync` (204/303, or 409 during cool-down)
  - `GET /folders?path=`, `POST /folders` (fields `path`, `new_folder_name`)

- [ ] **Step 1: Write the failing tests**

`tests/test_web_mappings.py`:
```python
from pathlib import Path

from sqlalchemy import select

from app.models import Mapping
from tests.conftest import wait_until

HX = {"HX-Request": "true"}
PLAYLIST = "https://www.youtube.com/playlist?list=PL1"


class FakeSyncer:
    def __init__(self):
        self.synced = []
        self.running = set()

    async def sync(self, mapping_id):
        self.synced.append(mapping_id)


def form(**kw):
    data = {"name": "Veritasium", "type": "show", "folder": "shows/Veritasium", "playlist_url": PLAYLIST,
            "show_title": "", "enabled": "on", "sync_interval_minutes": ""}
    data.update(kw)
    return data


def test_new_form_renders(client):
    page = client.get("/mappings/new").text
    assert "New mapping" in page and 'name="ov_max_height"' in page and "Use global (1080)" in page


def test_create_mapping_redirects_and_triggers_sync(client, app, session, config):
    app.state.syncer = FakeSyncer()
    response = client.post("/mappings", data=form(), follow_redirects=False)
    assert response.status_code == 303
    mapping = session.scalars(select(Mapping)).one()
    assert response.headers["location"] == f"/mappings/{mapping.id}"
    assert mapping.show_title == "Veritasium" and mapping.enabled
    assert (config.media_root / "shows" / "Veritasium").is_dir()
    wait_until(lambda: app.state.syncer.synced == [mapping.id])


def test_create_manual_mapping_with_override(client, app, session):
    app.state.syncer = FakeSyncer()
    client.post("/mappings", data=form(name="Mix", type="music_video", folder="music", playlist_url="",
                                       ov_max_height="720"), follow_redirects=False)
    mapping = session.scalars(select(Mapping)).one()
    assert mapping.playlist_url is None and mapping.overrides == {"max_height": 720} and mapping.show_title is None
    assert app.state.syncer.synced == []


def test_create_mapping_errors_rerender_form(client, make_mapping, config):
    response = client.post("/mappings", data=form(folder="../../etc"))
    assert response.status_code == 400 and "must be a folder inside" in response.text
    assert 'value="Veritasium"' in response.text
    make_mapping("Existing", folder=str(config.media_root / "shows"))
    response = client.post("/mappings", data=form())
    assert response.status_code == 400 and "overlaps" in response.text
    response = client.post("/mappings", data=form(sync_interval_minutes="abc"))
    assert response.status_code == 400 and "whole number" in response.text


def test_edit_mapping(client, app, session, config):
    app.state.syncer = FakeSyncer()
    client.post("/mappings", data=form(), follow_redirects=False)
    mapping = session.scalars(select(Mapping)).one()
    page = client.get(f"/mappings/{mapping.id}/edit").text
    assert 'value="shows/Veritasium"' in page
    response = client.post(f"/mappings/{mapping.id}/edit", data=form(show_title="Veri", enabled=""),
                           follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    mapping = session.get(Mapping, mapping.id)
    assert mapping.show_title == "Veri" and not mapping.enabled


def test_delete_mapping_with_files(client, app, session, config):
    app.state.syncer = FakeSyncer()
    client.post("/mappings", data=form(), follow_redirects=False)
    mapping = session.scalars(select(Mapping)).one()
    response = client.post(f"/mappings/{mapping.id}/delete", data={"delete_files": "on"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert not (config.media_root / "shows" / "Veritasium").exists()
    session.expire_all()
    assert session.scalars(select(Mapping)).all() == []


def test_sync_now_and_cooldown(client, app, make_mapping):
    app.state.syncer = FakeSyncer()
    mapping = make_mapping(playlist_url=PLAYLIST)
    assert client.post(f"/mappings/{mapping.id}/sync", headers=HX).status_code == 204
    wait_until(lambda: app.state.syncer.synced == [mapping.id])
    app.state.worker.enter_cooldown("429")
    response = client.post(f"/mappings/{mapping.id}/sync", headers=HX)
    assert response.status_code == 409 and "rate-limit" in response.text


def test_folder_picker(client, config):
    (config.media_root / "a" / "b").mkdir(parents=True)
    (config.media_root / ".hidden").mkdir()
    root = client.get("/folders").text
    assert ">📁 a<" in root.replace("\n", "") and ".hidden" not in root
    assert "📁 b" in client.get("/folders", params={"path": "a"}).text
    assert client.get("/folders", params={"path": "../.."}).status_code == 400
    assert client.get("/folders", params={"path": "missing"}).status_code == 404
    response = client.post("/folders", data={"path": "a", "new_folder_name": "new: one"})
    assert response.status_code == 200 and (config.media_root / "a" / "new one").is_dir()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_mappings.py -v`
Expected: FAIL (404 on `/mappings/new`).

- [ ] **Step 3: Implement `app/web/mappings.py`**

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from app import services
from app import settings as settings_mod
from app.models import Mapping
from app.naming import sanitize
from app.services import MappingInput, ServiceError
from app.web.deps import SessionDep, done, get_or_404, render, spawn

router = APIRouter()

OVERRIDE_FIELDS = [
    ("max_height", "Max resolution", "choice"),
    ("container", "Container", "choice"),
    ("vcodec", "Preferred codec", "choice"),
    ("subtitles", "Subtitles", "choice"),
    ("subtitle_langs", "Subtitle languages", "text"),
    ("subtitle_auto", "Auto-generated subtitles", "bool"),
    ("sponsorblock", "SponsorBlock", "choice"),
    ("sponsorblock_categories", "SponsorBlock categories", "text"),
]

NEW_VALUES: dict[str, Any] = {
    "name": "", "type": "show", "folder": "", "playlist_url": "", "show_title": "",
    "enabled": True, "sync_interval_minutes": "", "overrides": {},
}


def _form_to_input(form) -> MappingInput:
    interval = (form.get("sync_interval_minutes") or "").strip()
    try:
        interval_value = int(interval) if interval else None
    except ValueError as exc:
        raise ServiceError("Sync interval must be a whole number of minutes") from exc
    return MappingInput(
        name=form.get("name", ""),
        type=form.get("type", ""),
        folder=form.get("folder", ""),
        playlist_url=(form.get("playlist_url") or "").strip() or None,
        show_title=(form.get("show_title") or "").strip() or None,
        enabled=form.get("enabled") == "on",
        sync_interval_minutes=interval_value,
        overrides={key: form.get(f"ov_{key}", "") for key in settings_mod.OVERRIDABLE},
    )


def _values_from_form(form) -> dict[str, Any]:
    return {
        "name": form.get("name", ""), "type": form.get("type", "show"), "folder": form.get("folder", ""),
        "playlist_url": form.get("playlist_url", ""), "show_title": form.get("show_title", ""),
        "enabled": form.get("enabled") == "on", "sync_interval_minutes": form.get("sync_interval_minutes", ""),
        "overrides": {k: form.get(f"ov_{k}") for k in settings_mod.OVERRIDABLE if form.get(f"ov_{k}")},
    }


def _values_from_mapping(mapping: Mapping, media_root: Path) -> dict[str, Any]:
    folder = Path(mapping.folder)
    try:
        folder_value = folder.relative_to(media_root.resolve()).as_posix()
    except ValueError:
        folder_value = mapping.folder
    return {
        "name": mapping.name, "type": mapping.type, "folder": folder_value,
        "playlist_url": mapping.playlist_url or "", "show_title": mapping.show_title or "",
        "enabled": mapping.enabled, "sync_interval_minutes": mapping.sync_interval_minutes or "",
        "overrides": {k: v for k, v in (mapping.overrides or {}).items() if v is not None},
    }


def _form_page(request: Request, session, mapping: Mapping | None, values: dict, error: str | None = None,
               status_code: int = 200):
    return render(request, "mapping_form.html", {
        "mapping": mapping, "v": values, "error": error, "choices": settings_mod.CHOICES,
        "override_fields": OVERRIDE_FIELDS, "globals": settings_mod.get_all(session),
        "media_root": str(request.app.state.config.media_root),
    }, status_code=status_code)


@router.get("/mappings/new")
async def new_mapping(request: Request, session: SessionDep):
    return _form_page(request, session, None, dict(NEW_VALUES))


@router.post("/mappings")
async def create_mapping(request: Request, session: SessionDep):
    form = await request.form()
    try:
        mapping = services.create_mapping(session, request.app.state.config.media_root, _form_to_input(form))
    except ValueError as exc:
        session.rollback()
        return _form_page(request, session, None, _values_from_form(form), str(exc), 400)
    if mapping.playlist_url:
        spawn(request.app, request.app.state.syncer.sync(mapping.id))
    return RedirectResponse(f"/mappings/{mapping.id}", status_code=303)


@router.get("/mappings/{mapping_id}/edit")
async def edit_mapping(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    return _form_page(request, session, mapping, _values_from_mapping(mapping, request.app.state.config.media_root))


@router.post("/mappings/{mapping_id}/edit")
async def update_mapping(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    form = await request.form()
    try:
        services.update_mapping(session, request.app.state.config.media_root, mapping, _form_to_input(form))
    except ValueError as exc:
        session.rollback()
        return _form_page(request, session, mapping, _values_from_form(form), str(exc), 400)
    return RedirectResponse(f"/mappings/{mapping.id}", status_code=303)


@router.post("/mappings/{mapping_id}/delete")
async def delete_mapping(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    form = await request.form()
    request.app.state.worker.cancel_mapping(mapping_id)
    services.delete_mapping(session, request.app.state.config.media_root, mapping,
                            delete_files=form.get("delete_files") == "on")
    return RedirectResponse("/", status_code=303)


@router.post("/mappings/{mapping_id}/sync")
async def sync_now(request: Request, mapping_id: int, session: SessionDep):
    get_or_404(session, Mapping, mapping_id)
    if request.app.state.worker.cooldown_until is not None:
        return PlainTextResponse("Syncing is paused while YouTube rate-limits us", status_code=409)
    spawn(request.app, request.app.state.syncer.sync(mapping_id))
    return done(request, f"/mappings/{mapping_id}")


def _browse_dir(media_root: Path, rel: str) -> Path:
    root = media_root.resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise HTTPException(status_code=400, detail="Folder must be inside the media root")
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="Folder not found")
    return path


def _picker(request: Request, rel: str):
    root = request.app.state.config.media_root.resolve()
    current = _browse_dir(root, rel)
    rel_path = "" if current == root else current.relative_to(root).as_posix()
    subdirs = sorted((p.name for p in current.iterdir() if p.is_dir() and not p.name.startswith(".")), key=str.casefold)
    parent = None
    if current != root:
        parent = "" if current.parent == root else current.parent.relative_to(root).as_posix()
    return render(request, "partials/folder_picker.html",
                  {"rel": rel_path, "subdirs": subdirs, "parent": parent, "root": str(root)})


@router.get("/folders")
async def folders(request: Request, path: str = ""):
    return _picker(request, path)


@router.post("/folders")
async def create_folder(request: Request):
    form = await request.form()
    rel = form.get("path", "")
    base = _browse_dir(request.app.state.config.media_root, rel)
    name = sanitize(form.get("new_folder_name", ""), 120, fallback="")
    if not name:
        raise HTTPException(status_code=400, detail="Enter a folder name")
    (base / name).mkdir(exist_ok=True)
    return _picker(request, rel)
```

- [ ] **Step 4: Register the router in `app/main.py`**

Change the import line `from app.web import dashboard, sse` to `from app.web import dashboard, mappings, sse`. Change `for module in (dashboard, sse):` to `for module in (dashboard, sse, mappings):`.

- [ ] **Step 5: Write the templates**

`app/templates/mapping_form.html`:
```html
{% extends "base.html" %}
{% block title %}{{ ("Edit " ~ mapping.name) if mapping else "New mapping" }} · YT → Jellyfin{% endblock %}
{% block content %}
<div class="page-head"><h1>{{ "Edit mapping" if mapping else "New mapping" }}</h1></div>
{% if error %}<div class="banner error">{{ error }}</div>{% endif %}
<form class="panel form" method="post" action="{{ ('/mappings/%d/edit'|format(mapping.id)) if mapping else '/mappings' }}">
  <label>Name
    <input name="name" value="{{ v.name }}" required>
  </label>
  <label>Type
    {% if mapping %}
      <input type="hidden" name="type" value="{{ v.type }}">
      <input value="{{ 'Show' if v.type == 'show' else 'Music videos' }}" disabled>
      <small>The type cannot be changed after creation.</small>
    {% else %}
      <select name="type">
        <option value="show" {{ "selected" if v.type == "show" }}>Show: Show S01E01 Title</option>
        <option value="music_video" {{ "selected" if v.type == "music_video" }}>Music videos: Artist - Song</option>
      </select>
    {% endif %}
  </label>
  <label data-show-only>Show title
    <input name="show_title" value="{{ v.show_title or '' }}" placeholder="Defaults to the name">
    <small>Used in file names and as the series title in Jellyfin.</small>
  </label>
  <label>Folder <span class="hint">relative to {{ media_root }} (or an absolute path inside it)</span>
    <span class="row">
      <input id="folder" name="folder" value="{{ v.folder }}" required placeholder="youtube/shows/Veritasium">
      <button type="button" class="btn ghost" hx-get="/folders" hx-target="#folder-picker">Browse…</button>
    </span>
  </label>
  <div id="folder-picker"></div>
  <label>YouTube playlist URL
    <input name="playlist_url" value="{{ v.playlist_url or '' }}" placeholder="Leave empty for a manual list">
    <small>Sync only adds new videos. Your order, names and removals are never overwritten.</small>
  </label>
  <label>Sync interval (minutes)
    <input name="sync_interval_minutes" value="{{ v.sync_interval_minutes }}" inputmode="numeric" placeholder="Global default: {{ globals.sync_interval_minutes }}">
  </label>
  <label class="check"><input type="checkbox" name="enabled" {{ "checked" if v.enabled }}> Enabled (sync and download)</label>
  <details {{ "open" if v.overrides }}>
    <summary>Quality overrides for this mapping</summary>
    <div class="grid">
      {% for key, label, kind in override_fields %}
      {% set current = v.overrides.get(key) %}
      <label>{{ label }}
        {% if kind == "choice" %}
        <select name="ov_{{ key }}">
          <option value="">Use global ({{ globals[key] }})</option>
          {% for c in choices[key] %}
          <option value="{{ c }}" {{ "selected" if current is not none and current|string == c|string }}>{{ c }}</option>
          {% endfor %}
        </select>
        {% elif kind == "bool" %}
        <select name="ov_{{ key }}">
          <option value="">Use global ({{ "yes" if globals[key] else "no" }})</option>
          <option value="true" {{ "selected" if current is not none and current|string|lower == "true" }}>yes</option>
          <option value="false" {{ "selected" if current is not none and current|string|lower == "false" }}>no</option>
        </select>
        {% else %}
        <input name="ov_{{ key }}" value="{{ current if current is not none else '' }}" placeholder="Global: {{ globals[key] }}">
        {% endif %}
      </label>
      {% endfor %}
    </div>
  </details>
  <div class="actions">
    <button class="btn">{{ "Save" if mapping else "Create mapping" }}</button>
    <a class="btn ghost" href="{{ ('/mappings/%d'|format(mapping.id)) if mapping else '/' }}">Cancel</a>
  </div>
</form>
{% if mapping %}
<form class="panel danger-zone" method="post" action="/mappings/{{ mapping.id }}/delete"
      onsubmit="return confirm('Delete this mapping?')">
  <h2>Delete mapping</h2>
  <label class="check"><input type="checkbox" name="delete_files"> Also delete its folder and every downloaded file</label>
  <p><button class="btn danger">Delete mapping</button></p>
</form>
{% endif %}
{% endblock %}
```

`app/templates/partials/folder_picker.html`:
```html
<div class="picker">
  <div class="picker-head">
    <strong>{{ root }}{% if rel %}/{{ rel }}{% endif %}</strong>
    {% if parent is not none %}
    <button type="button" class="btn small ghost" hx-get="/folders?path={{ parent|urlencode }}" hx-target="#folder-picker">↑ Up</button>
    {% endif %}
    {% if rel %}<button type="button" class="btn small" data-pick-folder="{{ rel }}">Use this folder</button>{% endif %}
  </div>
  <ul class="picker-list">
    {% for name in subdirs %}
    {% set child = (rel ~ "/" ~ name) if rel else name %}
    <li>
      <button type="button" class="link" hx-get="/folders?path={{ child|urlencode }}" hx-target="#folder-picker">📁 {{ name }}</button>
      <button type="button" class="btn small ghost" data-pick-folder="{{ child }}">Use</button>
    </li>
    {% else %}
    <li class="muted">No subfolders</li>
    {% endfor %}
  </ul>
  <span class="inline">
    <input id="new-folder-name" name="new_folder_name" placeholder="New folder name">
    <button type="button" class="btn small" hx-post="/folders" hx-vals='{{ {"path": rel}|tojson }}'
            hx-include="#new-folder-name" hx-target="#folder-picker">Create</button>
  </span>
</div>
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_web_mappings.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add app/web/mappings.py app/main.py app/templates tests/test_web_mappings.py
git commit -m "feat: mapping create/edit/delete pages with folder picker"
```

---

### Task 16: Mapping detail page: live list, reorder, rename, seasons, add, delete, retry

**Files:**
- Create: `app/web/videos.py`
- Create: `app/templates/mapping_detail.html`, `app/templates/partials/video_list.html`, `app/templates/partials/video_row.html`, `app/templates/partials/video_edit_row.html`, `app/templates/partials/video_status.html`
- Modify: `app/main.py` (include the `videos` router)
- Test: `tests/test_web_videos.py`

**Interfaces:**
- Consumes: `services.reorder/set_season_break/rename_video/delete_video/retry_video/unignore`, `ServiceError` (Task 9); `organizer.ordered_videos/target_stems/naming_items` (Task 6); `naming.season_episode/episode_code` (Task 3); `settings.resolve` (Task 2); `Syncer.add_url`, `Syncer.running` (Task 11); `DownloadFailed` (Task 8); `Worker.cancel/kick` (Task 12)
- Produces:
  - `list_context(session, mapping) -> dict` with keys `mapping, videos, previews, codes, season_starts, breaks, first_id`
  - Routes: `GET /mappings/{id}`, `GET /mappings/{id}/list`, `POST /mappings/{id}/reorder` (`order="3,1,2"`), `POST /mappings/{id}/videos` (`url`), `POST /mappings/{id}/ignored/{youtube_id}/unignore`, `GET /videos/{id}/status`, `GET /videos/{id}/row`, `GET /videos/{id}/edit`, `POST /videos/{id}/rename` (`title` | `artist`+`song`), `POST /videos/{id}/season` (`on=1|0`), `POST /videos/{id}/delete` (`ignore=on`), `POST /videos/{id}/retry`
  - Validation errors return **422** with the re-rendered partial. Rename errors also set `HX-Retarget: #video-<id>` and `HX-Reswap: outerHTML`.

- [ ] **Step 1: Write the failing tests**

`tests/test_web_videos.py`:
```python
from pathlib import Path

from sqlalchemy import select

from app.models import IgnoredVideo, MappingType, SeasonBreak, Video, VideoStatus
from app.sync import Syncer
from app.ytdlp_client import Entry
from tests.conftest import FakeNotifier, FakeWorker

PLAYLIST = "https://www.youtube.com/playlist?list=PL1"


def done(mapping, add_video, yid, title):
    (Path(mapping.folder) / f"{yid}.mkv").write_text(yid)
    return add_video(mapping, yid, title=title, status=VideoStatus.DONE, file_path=f"{yid}.mkv")


def test_detail_page_lists_videos_with_previews(client, make_mapping, add_video):
    mapping = make_mapping(playlist_url=PLAYLIST)
    add_video(mapping, "a", title="First")
    add_video(mapping, "b", title="Second", status=VideoStatus.FAILED, error="boom")
    page = client.get(f"/mappings/{mapping.id}").text
    assert "First" in page and "Second" in page
    assert "Season 01/My Show S01E01 First.mkv" in page
    assert "S01E02" in page and "boom" in page and "Retry" in page
    assert f'data-sortable="/mappings/{mapping.id}/reorder"' in page
    assert client.get("/mappings/999").status_code == 404


def test_reorder(client, session, make_mapping, add_video):
    mapping = make_mapping()
    a = done(mapping, add_video, "a", "A")
    b = done(mapping, add_video, "b", "B")
    response = client.post(f"/mappings/{mapping.id}/reorder", data={"order": f"{b.id},{a.id}"})
    assert response.status_code == 200 and 'id="video-list"' in response.text
    session.expire_all()
    assert session.get(Video, b.id).position == 1
    bad = client.post(f"/mappings/{mapping.id}/reorder", data={"order": f"{a.id}"})
    assert bad.status_code == 422 and "exactly once" in bad.text


def test_season_toggle(client, session, make_mapping, add_video):
    mapping = make_mapping()
    add_video(mapping, "a")
    b = add_video(mapping, "b")
    response = client.post(f"/videos/{b.id}/season", data={"on": "1"})
    assert response.status_code == 200 and "Season 2" in response.text and "Remove season break" in response.text
    assert session.scalars(select(SeasonBreak)).one().video_id == b.id
    response = client.post(f"/videos/{b.id}/season", data={"on": "0"})
    assert "Season 2" not in response.text


def test_rename_show_video(client, session, make_mapping, add_video):
    mapping = make_mapping()
    video = done(mapping, add_video, "a", "Old")
    assert 'name="title"' in client.get(f"/videos/{video.id}/edit").text
    response = client.post(f"/videos/{video.id}/rename", data={"title": "Shiny"})
    assert response.status_code == 200 and "Shiny" in response.text
    assert (Path(mapping.folder) / "Season 01" / "My Show S01E01 Shiny.mkv").exists()
    assert f'id="video-{video.id}"' in client.get(f"/videos/{video.id}/row").text


def test_rename_music_video_validation(client, make_mapping, add_video):
    mapping = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    video = add_video(mapping, "m", artist="A", song="S")
    response = client.post(f"/videos/{video.id}/rename", data={"artist": "Queen", "song": ""})
    assert response.status_code == 422
    assert response.headers["HX-Retarget"] == f"#video-{video.id}" and "required" in response.text


def test_delete_with_ignore(client, session, make_mapping, add_video):
    mapping = make_mapping(playlist_url=PLAYLIST)
    video = done(mapping, add_video, "a", "A")
    response = client.post(f"/videos/{video.id}/delete", data={"ignore": "on"})
    assert response.status_code == 200 and "No videos yet" in response.text
    session.expire_all()
    assert session.get(Video, video.id) is None
    assert session.scalars(select(IgnoredVideo)).one().youtube_id == "a"
    page = client.get(f"/mappings/{mapping.id}").text
    assert "Ignored videos (1)" in page
    response = client.post(f"/mappings/{mapping.id}/ignored/a/unignore", follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    assert session.scalars(select(IgnoredVideo)).all() == []


def test_retry_and_status(client, session, make_mapping, add_video):
    mapping = make_mapping()
    video = add_video(mapping, "a", status=VideoStatus.FAILED, attempts=2, error="boom")
    assert client.post(f"/videos/{video.id}/retry").status_code == 200
    session.expire_all()
    assert session.get(Video, video.id).status == VideoStatus.PENDING
    assert "Queued" in client.get(f"/videos/{video.id}/status").text


def test_add_url(client, app, config, make_mapping):
    mapping = make_mapping()
    app.state.syncer = Syncer(app.state.session_factory, config, FakeWorker(), FakeNotifier(),
                              fetch=lambda url, opts, cookie: [Entry("new", "New video")])
    response = client.post(f"/mappings/{mapping.id}/videos", data={"url": "https://youtu.be/new"})
    assert response.status_code == 200 and "New video" in response.text and "Added 1 video" in response.text
    response = client.post(f"/mappings/{mapping.id}/videos", data={"url": "nope"})
    assert response.status_code == 422 and "Enter a YouTube" in response.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_videos.py -v`
Expected: FAIL (404 on `/mappings/{id}`).

- [ ] **Step 3: Implement `app/web/videos.py`**

```python
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import naming, organizer, services
from app import settings as settings_mod
from app.models import Mapping, MappingType, Video
from app.services import ServiceError
from app.web.deps import SessionDep, get_or_404, render
from app.ytdlp_client import DownloadFailed

router = APIRouter()


def list_context(session, mapping: Mapping) -> dict:
    values = settings_mod.resolve(session, mapping)
    videos = organizer.ordered_videos(mapping)
    stems = organizer.target_stems(mapping, values["max_filename_length"])
    numbers = naming.season_episode(organizer.naming_items(mapping)) if mapping.type == MappingType.SHOW else {}
    previews = {}
    for video in videos:
        if video.id in stems:
            ext = Path(video.file_path).suffix if video.file_path else f".{values['container']}"
            previews[video.id] = stems[video.id] + ext
    return {
        "mapping": mapping,
        "videos": videos,
        "previews": previews,
        "codes": {vid: naming.episode_code(*n) for vid, n in numbers.items()},
        "season_starts": {vid: n[0] for vid, n in numbers.items() if n[1] == 1},
        "breaks": {b.video_id for b in mapping.season_breaks},
        "first_id": videos[0].id if videos else None,
    }


def _list(request: Request, session, mapping: Mapping, *, error: str | None = None,
          notice: str | None = None, status_code: int = 200):
    context = list_context(session, mapping) | {"error": error, "notice": notice}
    return render(request, "partials/video_list.html", context, status_code=status_code)


def _media_root(request: Request) -> Path:
    return request.app.state.config.media_root


@router.get("/mappings/{mapping_id}")
async def detail(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    context = list_context(session, mapping) | {"syncing": mapping_id in request.app.state.syncer.running}
    return render(request, "mapping_detail.html", context)


@router.get("/mappings/{mapping_id}/list")
async def video_list(request: Request, mapping_id: int, session: SessionDep):
    return _list(request, session, get_or_404(session, Mapping, mapping_id))


@router.post("/mappings/{mapping_id}/reorder")
async def reorder(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    form = await request.form()
    try:
        ids = [int(part) for part in str(form.get("order", "")).split(",") if part.strip()]
        services.reorder(session, _media_root(request), mapping, ids)
    except ValueError as exc:
        session.rollback()
        message = str(exc) if isinstance(exc, ServiceError) else "Invalid order"
        return _list(request, session, mapping, error=message, status_code=422)
    return _list(request, session, mapping)


@router.post("/mappings/{mapping_id}/videos")
async def add_videos(request: Request, mapping_id: int, session: SessionDep):
    mapping = get_or_404(session, Mapping, mapping_id)
    form = await request.form()
    try:
        added = await request.app.state.syncer.add_url(mapping_id, str(form.get("url", "")))
    except (ServiceError, DownloadFailed) as exc:
        return _list(request, session, mapping, error=str(exc), status_code=422)
    session.expire_all()
    mapping = get_or_404(session, Mapping, mapping_id)
    notice = f"Added {added} video(s)" if added else "Nothing new to add (already in this mapping)"
    return _list(request, session, mapping, notice=notice)


@router.post("/mappings/{mapping_id}/ignored/{youtube_id}/unignore")
async def unignore(request: Request, mapping_id: int, youtube_id: str, session: SessionDep):
    services.unignore(session, get_or_404(session, Mapping, mapping_id), youtube_id)
    return RedirectResponse(f"/mappings/{mapping_id}", status_code=303)


@router.get("/videos/{video_id}/status")
async def status(request: Request, video_id: int, session: SessionDep):
    return render(request, "partials/video_status.html", {"v": get_or_404(session, Video, video_id)})


@router.get("/videos/{video_id}/row")
async def row(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    return render(request, "partials/video_row.html", list_context(session, video.mapping) | {"v": video})


@router.get("/videos/{video_id}/edit")
async def edit_row(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    return render(request, "partials/video_edit_row.html", {"v": video, "mapping": video.mapping})


@router.post("/videos/{video_id}/rename")
async def rename(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    form = await request.form()
    try:
        services.rename_video(session, _media_root(request), video, title=form.get("title"),
                              artist=form.get("artist"), song=form.get("song"))
    except ServiceError as exc:
        session.rollback()
        response = render(request, "partials/video_edit_row.html",
                          {"v": video, "mapping": video.mapping, "error": str(exc)}, status_code=422)
        response.headers["HX-Retarget"] = f"#video-{video.id}"
        response.headers["HX-Reswap"] = "outerHTML"
        return response
    return _list(request, session, video.mapping)


@router.post("/videos/{video_id}/season")
async def season(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    mapping = video.mapping
    form = await request.form()
    try:
        services.set_season_break(session, _media_root(request), mapping, video.id, form.get("on") == "1")
    except ServiceError as exc:
        session.rollback()
        return _list(request, session, mapping, error=str(exc), status_code=422)
    return _list(request, session, mapping)


@router.post("/videos/{video_id}/delete")
async def delete(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    mapping = video.mapping
    form = await request.form()
    request.app.state.worker.cancel(video.id)
    services.delete_video(session, _media_root(request), video, ignore=form.get("ignore") == "on")
    return _list(request, session, mapping)


@router.post("/videos/{video_id}/retry")
async def retry(request: Request, video_id: int, session: SessionDep):
    video = get_or_404(session, Video, video_id)
    services.retry_video(session, video)
    request.app.state.worker.kick()
    return _list(request, session, video.mapping)
```

- [ ] **Step 4: Register the router in `app/main.py`**

Change `from app.web import dashboard, mappings, sse` to `from app.web import dashboard, mappings, sse, videos`. Change the loop to `for module in (dashboard, sse, mappings, videos):`.

- [ ] **Step 5: Write the templates**

`app/templates/mapping_detail.html`:
```html
{% extends "base.html" %}
{% block title %}{{ mapping.name }} · YT → Jellyfin{% endblock %}
{% block content %}
<div class="page-head">
  <div>
    <h1>{{ mapping.name }}</h1>
    <div class="muted">{{ "Show" if mapping.type == "show" else "Music videos" }} · <code>{{ mapping.folder }}</code></div>
    {% if mapping.playlist_url %}
    <div class="muted">
      Playlist: <a href="{{ mapping.playlist_url }}" target="_blank" rel="noopener">{{ mapping.playlist_url }}</a>
      · last sync {{ mapping.last_synced_at|localtime }}
      {% if mapping.last_sync_error %}· <span class="error-text">{{ mapping.last_sync_error }}</span>{% endif %}
    </div>
    {% else %}
    <div class="muted">Manual list (no playlist)</div>
    {% endif %}
  </div>
  <div class="actions">
    {% if mapping.playlist_url %}
    <span class="sync-status muted" sse-swap="sync-{{ mapping.id }}">{% if syncing %}Checking playlist…{% endif %}</span>
    <button class="btn" hx-post="/mappings/{{ mapping.id }}/sync" hx-swap="none">Sync now</button>
    {% endif %}
    <a class="btn ghost" href="/mappings/{{ mapping.id }}/edit">Edit</a>
  </div>
</div>
<form class="panel add-video" hx-post="/mappings/{{ mapping.id }}/videos" hx-target="#video-list" hx-swap="outerHTML"
      hx-on::after-request="if (event.detail.successful) this.reset()">
  <input name="url" placeholder="Paste a YouTube video or playlist URL to add it here" required>
  <button class="btn">Add</button>
  <span class="htmx-indicator muted">Fetching…</span>
</form>
{% include "partials/video_list.html" %}
{% if mapping.ignored %}
<details class="panel">
  <summary>Ignored videos ({{ mapping.ignored|length }})</summary>
  <p class="muted">You removed these. Sync will not add them again.</p>
  <ul class="ignored">
    {% for item in mapping.ignored %}
    <li>
      <a href="https://youtu.be/{{ item.youtube_id }}" target="_blank" rel="noopener">{{ item.title or item.youtube_id }}</a>
      <form method="post" action="/mappings/{{ mapping.id }}/ignored/{{ item.youtube_id }}/unignore" class="inline">
        <button class="btn small ghost">Un-ignore</button>
      </form>
    </li>
    {% endfor %}
  </ul>
</details>
{% endif %}
{% endblock %}
```

`app/templates/partials/video_list.html`:
```html
<div id="video-list" hx-get="/mappings/{{ mapping.id }}/list" hx-trigger="sse:list-{{ mapping.id }}" hx-swap="outerHTML">
  {% if error %}<div class="banner error">{{ error }}</div>{% endif %}
  {% if notice %}<div class="banner info">{{ notice }}</div>{% endif %}
  {% if videos %}
  <ol class="videos" data-sortable="/mappings/{{ mapping.id }}/reorder">
    {% for v in videos %}
      {% if v.id in season_starts %}<li class="season-header">Season {{ season_starts[v.id] }}</li>{% endif %}
      {% include "partials/video_row.html" %}
    {% endfor %}
  </ol>
  {% else %}
  <div class="empty">No videos yet. {{ "Run a sync or paste" if mapping.playlist_url else "Paste" }} a YouTube URL above.</div>
  {% endif %}
</div>
```

`app/templates/partials/video_row.html`:
```html
<li class="video status-{{ v.status }}" id="video-{{ v.id }}" data-video-id="{{ v.id }}">
  <span class="drag" title="Drag to reorder">⠿</span>
  <img class="thumb" loading="lazy" alt="" src="https://i.ytimg.com/vi/{{ v.youtube_id }}/mqdefault.jpg">
  <div class="meta">
    <div class="title">
      {% if mapping.type == "show" %}
        {% if v.id in codes %}<span class="code">{{ codes[v.id] }}</span>{% endif %}{{ v.display_title }}
        {% if v.custom_title %}<span class="muted" title="YouTube title: {{ v.yt_title }}">(renamed)</span>{% endif %}
      {% else %}
        <strong>{{ v.artist or "?" }}</strong> — {{ v.song or v.yt_title }}
      {% endif %}
    </div>
    <div class="filename" title="File name in Jellyfin">{{ previews.get(v.id, "not numbered (unavailable)") }}</div>
    <a class="yt-link" href="https://youtu.be/{{ v.youtube_id }}" target="_blank" rel="noopener">youtu.be/{{ v.youtube_id }}</a>
    {% if v.source == "manual" %}<span class="tag">manual</span>{% endif %}
  </div>
  <div class="status" hx-get="/videos/{{ v.id }}/status" hx-trigger="sse:video-{{ v.id }}">{% include "partials/video_status.html" %}</div>
  <div class="row-actions">
    <button class="btn small ghost" hx-get="/videos/{{ v.id }}/edit" hx-target="#video-{{ v.id }}" hx-swap="outerHTML">Edit</button>
    {% if mapping.type == "show" and v.id != first_id %}
      {% if v.id in breaks %}
      <button class="btn small ghost" hx-post="/videos/{{ v.id }}/season" hx-vals='{"on": "0"}' hx-target="#video-list" hx-swap="outerHTML">Remove season break</button>
      {% else %}
      <button class="btn small ghost" hx-post="/videos/{{ v.id }}/season" hx-vals='{"on": "1"}' hx-target="#video-list" hx-swap="outerHTML">New season here</button>
      {% endif %}
    {% endif %}
    {% if v.status in ("failed", "unavailable") %}
    <button class="btn small" hx-post="/videos/{{ v.id }}/retry" hx-target="#video-list" hx-swap="outerHTML">Retry</button>
    {% endif %}
    {% if mapping.playlist_url %}
    <button class="btn small danger" hx-post="/videos/{{ v.id }}/delete" hx-vals='{"ignore": "on"}' hx-target="#video-list" hx-swap="outerHTML"
            hx-confirm="Delete this video{{ ' and its files' if v.file_path }}? Sync will not add it back.">Delete</button>
    {% else %}
    <button class="btn small danger" hx-post="/videos/{{ v.id }}/delete" hx-target="#video-list" hx-swap="outerHTML"
            hx-confirm="Delete this video{{ ' and its files' if v.file_path }}?">Delete</button>
    {% endif %}
  </div>
</li>
```

`app/templates/partials/video_edit_row.html`:
```html
<li class="video editing" id="video-{{ v.id }}" data-video-id="{{ v.id }}">
  <form class="edit-form" hx-post="/videos/{{ v.id }}/rename" hx-target="#video-list" hx-swap="outerHTML">
    {% if error %}<div class="banner error">{{ error }}</div>{% endif %}
    {% if mapping.type == "show" %}
      <label>Title <input name="title" value="{{ v.custom_title or '' }}" placeholder="{{ v.yt_title }}"></label>
      <small class="muted">Leave empty to use the YouTube title.</small>
    {% else %}
      <label>Artist <input name="artist" value="{{ v.artist or '' }}"></label>
      <label>Song <input name="song" value="{{ v.song or '' }}"></label>
    {% endif %}
    <button class="btn small">Save</button>
    <button type="button" class="btn small ghost" hx-get="/videos/{{ v.id }}/row" hx-target="#video-{{ v.id }}" hx-swap="outerHTML">Cancel</button>
  </form>
</li>
```

`app/templates/partials/video_status.html`:
```html
{% if v.status == "downloading" %}
<div class="progress-wrap" sse-swap="progress-{{ v.id }}">
  <div class="progress indeterminate"><div class="bar"></div></div>
  <span class="progress-text">Starting…</span>
</div>
{% elif v.status == "done" %}
<span class="badge done">Downloaded</span>
{% elif v.status == "pending" %}
<span class="badge pending">Queued</span>
{% elif v.status == "failed" %}
<span class="badge failed">Failed{% if v.attempts %} ×{{ v.attempts }}{% endif %}</span>
{% if v.next_retry_at %}<span class="muted progress-text">retry in <span data-countdown="{{ v.next_retry_at|isoutc }}"></span></span>{% endif %}
<div class="error-text" title="{{ v.error }}">{{ (v.error or "")|truncate(140) }}</div>
{% elif v.status == "unavailable" %}
<span class="badge unavailable">Unavailable</span>
<div class="error-text" title="{{ v.error }}">{{ (v.error or "")|truncate(140) }}</div>
{% endif %}
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_web_videos.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add app/web/videos.py app/main.py app/templates tests/test_web_videos.py
git commit -m "feat: mapping detail page with live list, drag reorder, seasons, rename and delete"
```

---

### Task 17: Settings page, cookies upload and yt-dlp update button

**Files:**
- Create: `app/web/settings_page.py`, `app/templates/settings.html`
- Modify: `app/main.py` (include the `settings_page` router)
- Test: `tests/test_web_settings.py`

**Interfaces:**
- Consumes: `settings.DEFAULTS/CHOICES/BOOL_KEYS/get_all/set_many/SettingsError` (Task 2); `updater.running_version/installed_version`, `Updater.check/last_result/last_checked_at` (Task 13); `app.state.worker.kick()`
- Produces:
  - `looks_like_cookies(text: str) -> bool`
  - Routes: `GET /settings` (shows `?msg=`), `POST /settings`, `POST /settings/cookies` (multipart field `file`), `POST /settings/cookies/delete`, `POST /settings/ytdlp/update`

- [ ] **Step 1: Write the failing tests**

`tests/test_web_settings.py`:
```python
import stat

from app import settings as settings_mod

COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tf6=40000000\n"


def test_settings_page_shows_values_and_versions(client):
    page = client.get("/settings").text
    assert 'name="max_height"' in page and '<option value="1080" selected' in page
    assert "yt-dlp" in page and "No cookies uploaded" in page


def test_save_settings(client, session):
    response = client.post("/settings", data={"max_height": "720", "min_delay_seconds": "5",
                                              "max_delay_seconds": "10", "retry_backoff_minutes": "1, 2"},
                           follow_redirects=False)
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    session.expire_all()
    values = settings_mod.get_all(session)
    assert values["max_height"] == 720 and values["retry_backoff_minutes"] == [1, 2]
    assert values["ytdlp_auto_update"] is False  # unchecked checkbox = off


def test_save_settings_validation_error(client, session):
    response = client.post("/settings", data={"min_delay_seconds": "100", "max_delay_seconds": "10"})
    assert response.status_code == 400 and "Minimum delay" in response.text
    assert 'value="100"' in response.text
    assert settings_mod.get_all(session)["min_delay_seconds"] == 30


def test_cookies_upload_and_delete(client, config):
    response = client.post("/settings/cookies", files={"file": ("cookies.txt", COOKIES, "text/plain")},
                           follow_redirects=False)
    assert response.status_code == 303
    assert config.cookies_path.read_text() == COOKIES
    assert stat.S_IMODE(config.cookies_path.stat().st_mode) == 0o600
    assert "Cookies uploaded" in client.get("/settings").text
    bad = client.post("/settings/cookies", files={"file": ("x.txt", "hello", "text/plain")})
    assert bad.status_code == 400 and "Netscape" in bad.text
    client.post("/settings/cookies/delete")
    assert not config.cookies_path.exists()


def test_ytdlp_update_button(client, app):
    class FakeUpdater:
        last_result = None
        last_checked_at = None

        async def check(self):
            return "Up to date (2026.01.01)"

    app.state.updater = FakeUpdater()
    response = client.post("/settings/ytdlp/update", follow_redirects=False)
    assert response.status_code == 303 and "Up+to+date" in response.headers["location"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_settings.py -v`
Expected: FAIL (404 on `/settings`).

- [ ] **Step 3: Implement `app/web/settings_page.py`**

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import RedirectResponse

from app import settings as settings_mod
from app import updater as updater_mod
from app.settings import SettingsError
from app.web.deps import SessionDep, render

router = APIRouter()


def looks_like_cookies(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    if lines[0].startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        return True
    data = [line for line in lines if not line.startswith("#")]
    return bool(data) and all(len(line.split("\t")) == 7 for line in data)


def _installed_version() -> str:
    try:
        return updater_mod.installed_version()
    except Exception:  # noqa: BLE001
        return "unknown"


def _page(request: Request, session, *, values=None, error=None, status_code=200):
    config = request.app.state.config
    cookie_file = config.cookiefile()
    updater = request.app.state.updater
    return render(request, "settings.html", {
        "v": values or settings_mod.get_all(session),
        "choices": settings_mod.CHOICES,
        "error": error,
        "notice": request.query_params.get("msg"),
        "ytdlp_running": updater_mod.running_version(),
        "ytdlp_installed": _installed_version(),
        "last_update": updater.last_result,
        "last_checked": updater.last_checked_at,
        "cookies_present": cookie_file is not None,
        "cookies_mtime": datetime.fromtimestamp(cookie_file.stat().st_mtime, UTC).replace(tzinfo=None) if cookie_file else None,
    }, status_code=status_code)


def _redirect(message: str) -> RedirectResponse:
    return RedirectResponse("/settings?" + urlencode({"msg": message}), status_code=303)


@router.get("/settings")
async def settings_page(request: Request, session: SessionDep):
    return _page(request, session)


@router.post("/settings")
async def save_settings(request: Request, session: SessionDep):
    form = await request.form()
    raw = {key: form[key] for key in settings_mod.DEFAULTS if key in form and key not in settings_mod.BOOL_KEYS}
    for key in settings_mod.BOOL_KEYS:
        raw[key] = key in form
    try:
        settings_mod.set_many(session, raw)
    except SettingsError as exc:
        session.rollback()
        return _page(request, session, values={**settings_mod.get_all(session), **raw}, error=str(exc), status_code=400)
    request.app.state.worker.kick()
    return _redirect("Settings saved")


@router.post("/settings/cookies")
async def upload_cookies(request: Request, session: SessionDep, file: Annotated[UploadFile, File()]):
    text = (await file.read()).decode("utf-8", errors="replace")
    if not looks_like_cookies(text):
        return _page(request, session, error="That does not look like a Netscape-format cookies.txt file", status_code=400)
    path = request.app.state.config.cookies_path
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return _redirect("Cookies saved")


@router.post("/settings/cookies/delete")
async def delete_cookies(request: Request):
    request.app.state.config.cookies_path.unlink(missing_ok=True)
    return _redirect("Cookies removed")


@router.post("/settings/ytdlp/update")
async def update_ytdlp(request: Request):
    return _redirect(await request.app.state.updater.check())
```

- [ ] **Step 4: Register the router in `app/main.py`**

Change `from app.web import dashboard, mappings, sse, videos` to `from app.web import dashboard, mappings, settings_page, sse, videos`. Change the loop to `for module in (dashboard, sse, mappings, videos, settings_page):`.

- [ ] **Step 5: Write `app/templates/settings.html`**

```html
{% extends "base.html" %}
{% block title %}Settings · YT → Jellyfin{% endblock %}
{% block content %}
{% macro select(name, label, help="") -%}
<label>{{ label }}
  <select name="{{ name }}">
    {% for c in choices[name] %}<option value="{{ c }}" {{ "selected" if v[name]|string == c|string }}>{{ c }}</option>{% endfor %}
  </select>
  {% if help %}<small>{{ help }}</small>{% endif %}
</label>
{%- endmacro %}
{% macro number(name, label, help="", step="1") -%}
<label>{{ label }}
  <input type="number" name="{{ name }}" value="{{ v[name] }}" step="{{ step }}" min="0">
  {% if help %}<small>{{ help }}</small>{% endif %}
</label>
{%- endmacro %}
{% macro text(name, label, help="") -%}
<label>{{ label }}
  <input name="{{ name }}" value="{{ v[name] }}">
  {% if help %}<small>{{ help }}</small>{% endif %}
</label>
{%- endmacro %}
{% macro check(name, label) -%}
<label class="check"><input type="checkbox" name="{{ name }}" {{ "checked" if v[name] in (true, "on", "true") }}> {{ label }}</label>
{%- endmacro %}
<div class="page-head"><h1>Settings</h1></div>
{% if notice %}<div class="banner info">{{ notice }}</div>{% endif %}
{% if error %}<div class="banner error">{{ error }}</div>{% endif %}
<form class="settings" method="post" action="/settings">
  <fieldset><legend>Quality</legend>
    <div class="grid">
      {{ select("max_height", "Max resolution") }}
      {{ select("container", "Container", "mkv is the most flexible; mp4 plays everywhere") }}
      {{ select("vcodec", "Preferred video codec", "h264 for old TVs; falls back if unavailable") }}
    </div>
  </fieldset>
  <fieldset><legend>Subtitles &amp; SponsorBlock</legend>
    <div class="grid">
      {{ select("subtitles", "Subtitles", "embed into the file, or save .srt next to it") }}
      {{ text("subtitle_langs", "Subtitle languages", "comma separated, e.g. en,de") }}
      {{ check("subtitle_auto", "Include auto-generated subtitles") }}
      {{ select("sponsorblock", "SponsorBlock", "mark segments as chapters, or cut them out") }}
      {{ text("sponsorblock_categories", "SponsorBlock categories") }}
    </div>
  </fieldset>
  <fieldset><legend>Sync</legend>
    <div class="grid">
      {{ number("sync_interval_minutes", "Default sync interval (minutes)") }}
      {{ number("sync_jitter_seconds", "Random pause between playlist checks (max seconds)") }}
    </div>
  </fieldset>
  <fieldset><legend>Throttling (avoid YouTube bans)</legend>
    <div class="grid">
      {{ number("min_delay_seconds", "Delay between downloads: min (s)") }}
      {{ number("max_delay_seconds", "Delay between downloads: max (s)") }}
      {{ number("sleep_requests", "Pause between requests (s)", "", "0.1") }}
      {{ number("sleep_interval", "yt-dlp sleep before each download: min (s)") }}
      {{ number("max_sleep_interval", "yt-dlp sleep before each download: max (s)") }}
      {{ number("ratelimit_kbps", "Download rate limit (KiB/s, 0 = unlimited)") }}
      {{ number("concurrency", "Concurrent downloads") }}
      {{ number("cooldown_minutes", "Cool-down after rate limit (minutes)", "doubles on repeated hits") }}
      {{ number("cooldown_max_minutes", "Maximum cool-down (minutes)") }}
    </div>
  </fieldset>
  <fieldset><legend>Retries &amp; naming</legend>
    <div class="grid">
      <label>Retry backoff (minutes)
        <input name="retry_backoff_minutes" value="{{ v.retry_backoff_minutes|join(', ') if v.retry_backoff_minutes is not string else v.retry_backoff_minutes }}">
        <small>wait before each automatic retry; after the last one a video stays failed</small>
      </label>
      {{ number("max_filename_length", "Max file name length") }}
    </div>
  </fieldset>
  <fieldset><legend>yt-dlp</legend>
    {{ check("ytdlp_auto_update", "Update yt-dlp automatically (at start and daily)") }}
    <p class="muted">Running {{ ytdlp_running }} · installed {{ ytdlp_installed }}
      {% if last_update %}· {{ last_update }} ({{ last_checked|localtime }}){% endif %}</p>
  </fieldset>
  <div class="actions"><button class="btn">Save settings</button></div>
</form>
<div class="panel">
  <h2>yt-dlp update</h2>
  <form method="post" action="/settings/ytdlp/update"><button class="btn ghost">Check for update now</button></form>
</div>
<div class="panel">
  <h2>YouTube cookies</h2>
  <p class="muted">Upload a Netscape-format <code>cookies.txt</code> (export it from a logged-in browser) to fix
    age-restricted videos and "confirm you're not a bot" errors. Use a throwaway account.</p>
  {% if cookies_present %}
  <p>Cookies uploaded {{ cookies_mtime|localtime }}.</p>
  <form method="post" action="/settings/cookies/delete"><button class="btn danger small">Remove cookies</button></form>
  {% else %}
  <p class="muted">No cookies uploaded.</p>
  {% endif %}
  <form method="post" action="/settings/cookies" enctype="multipart/form-data" class="row">
    <input type="file" name="file" accept=".txt,text/plain" required>
    <button class="btn">Upload</button>
  </form>
</div>
{% endblock %}
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_web_settings.py -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add app/web/settings_page.py app/main.py app/templates/settings.html tests/test_web_settings.py
git commit -m "feat: settings page with throttling, cookies upload and yt-dlp update"
```

---

### Task 18: Docker image, compose example, README, integration test, end-to-end check

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `docker/entrypoint.sh`, `docker-compose.yml`, `README.md`
- Test: `tests/test_integration.py`

**Interfaces:**
- Consumes: `python -m app` (Task 14) with exit code 3 meaning restart; `/healthz`

- [ ] **Step 1: Write the integration test (opt-in, real network)**

`tests/test_integration.py`:
```python
import pytest

from app import settings as settings_mod
from app import ytdlp_client


@pytest.mark.integration
def test_real_download_of_short_video(tmp_path):
    values = dict(settings_mod.DEFAULTS, max_height=480, sleep_requests=0)
    events = []
    info, media, extras = ytdlp_client.download("jNQXAC9IVRw", values, tmp_path / "stage", None,
                                                events.append, lambda: False)
    assert info.youtube_id == "jNQXAC9IVRw" and info.title
    assert media.exists() and media.suffix == ".mkv" and media.stat().st_size > 10_000
    assert any(p.suffix == ".jpg" for p in extras)
    assert events
```

Run: `.venv/bin/pytest -m integration -v`
Expected: PASS when ffmpeg is installed and the network is reachable. If YouTube refuses the request, the error message is the classified yt-dlp error. Record the result; it is not part of the default suite.

- [ ] **Step 2: Write `docker/entrypoint.sh`**

```sh
#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
  groupadd -o -g "$PGID" app 2>/dev/null || true
  useradd -o -u "$PUID" -g "$PGID" -M -d /config -s /usr/sbin/nologin app 2>/dev/null || true
  mkdir -p /config /media
  chown "$PUID:$PGID" /config
  # The app updates yt-dlp in place, so the venv must be writable by the runtime user.
  if [ "$(stat -c %u /opt/venv)" != "$PUID" ]; then
    chown -R "$PUID:$PGID" /opt/venv
  fi
  exec gosu "$PUID:$PGID" "$0" "$@"
fi

umask 002
stopping=0
child=0
term() {
  stopping=1
  [ "$child" -ne 0 ] && kill -TERM "$child" 2>/dev/null || true
}
trap term TERM INT

while :; do
  python -m app &
  child=$!
  set +e
  wait "$child"
  code=$?
  if [ "$stopping" = 1 ]; then
    wait "$child" 2>/dev/null
    exit 0
  fi
  set -e
  if [ "$code" -ne 3 ]; then
    exit "$code"
  fi
  echo "yt-dlp was updated; restarting the app..."
done
```

- [ ] **Step 3: Write `Dockerfile` and `.dockerignore`**

`Dockerfile`:
```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    MEDIA_ROOT=/media \
    CONFIG_DIR=/config \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN pip install . && deno --version

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/media"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/healthz', timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
```

`.dockerignore`:
```
.venv
.git
**/__pycache__
.pytest_cache
tests
docs
config
media
```

- [ ] **Step 4: Write `docker-compose.yml`**

```yaml
services:
  yt-jellyfin-sync:
    build: .
    image: yt-jellyfin-sync:latest
    container_name: yt-jellyfin-sync
    init: true
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      PUID: "1000"
      PGID: "1000"
      TZ: "Europe/Bratislava"
      # AUTH_USER: "admin"
      # AUTH_PASSWORD: "change-me"
    volumes:
      - ./config:/config
      - /srv/media:/media          # the same folder Jellyfin sees

  jellyfin:
    image: jellyfin/jellyfin:latest
    container_name: jellyfin
    restart: unless-stopped
    ports:
      - "8096:8096"
    volumes:
      - ./jellyfin-config:/config
      - ./jellyfin-cache:/cache
      - /srv/media:/media          # identical path inside both containers keeps things simple
```

- [ ] **Step 5: Write `README.md`**

````markdown
# YouTube → Jellyfin Sync

Self-hosted service that keeps YouTube playlists (or hand-picked lists) in your Jellyfin library,
named the way Jellyfin expects:

- **Shows:** `Season 01/Show S01E01 Video Title.mkv` + `.nfo` + `-thumb.jpg` + `tvshow.nfo`
- **Music videos:** `Artist - Song.mkv` + `.nfo` + `-thumb.jpg`

## Run

```bash
docker compose up -d --build
```

Open http://localhost:8080 and create a mapping: pick a type, a folder inside `/media`, and optionally a
playlist URL. Leave the URL empty for a manual list and paste video URLs on the mapping page.

| Variable | Default | Meaning |
|---|---|---|
| `PUID` / `PGID` | 1000 | Owner of downloaded files (match Jellyfin's user) |
| `TZ` | UTC | Time zone for the UI |
| `AUTH_USER` / `AUTH_PASSWORD` | unset | Enable HTTP basic auth |
| `PORT` | 8080 | Web UI port |

Volumes: `/config` (database, cookies, logs) and `/media` (shared with Jellyfin).

## Jellyfin setup

1. Create a **Shows** library pointing at the folder that holds your show mappings
   (e.g. `/media/youtube/shows`), and a **Music Videos** library for music-video mappings.
2. In each library: enable the **Nfo** metadata reader. Disable online metadata downloaders
   (TheMovieDb, TheTVDB, …) for these libraries so YouTube channels are not matched to unrelated shows.
3. Enable **real-time monitoring** so new downloads show up without a manual scan.

## Behaviour

- Sync only **adds** new playlist videos at the end. Your order, renames, season breaks and removals
  always win. Deleting a video from a playlist-backed mapping remembers it as *ignored*.
- Episode numbers follow the list order. Reordering, deleting or adding a season break renames the
  files on disk. Unavailable videos don't use up a number.
- Downloads are throttled with a random delay (default 30–90 s) and request pacing. On HTTP 429 or
  "confirm you're not a bot", the whole queue pauses (60 min, doubling up to 12 h).
- yt-dlp updates itself at start and daily; the app restarts itself once nothing is downloading.
- Age-restricted or bot-checked videos: upload a `cookies.txt` on the Settings page.

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -e '.[dev]'
.venv/bin/pytest                    # unit + web tests
.venv/bin/pytest -m integration     # downloads a real 19-second video
MEDIA_ROOT=./media CONFIG_DIR=./config .venv/bin/python -m app
```
````

- [ ] **Step 6: Run the full test suite**

Run: `.venv/bin/pytest -v`
Expected: every test PASSES (the integration test is deselected).

- [ ] **Step 7: Build and smoke-test the image**

```bash
docker build -t yt-jellyfin-sync:dev .
mkdir -p /tmp/ytjs-smoke/config /tmp/ytjs-smoke/media
docker run -d --rm --name ytjs-smoke -p 18080:8080 -e PUID="$(id -u)" -e PGID="$(id -g)" \
  -v /tmp/ytjs-smoke/config:/config -v /tmp/ytjs-smoke/media:/media yt-jellyfin-sync:dev
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:18080/healthz && break; sleep 1; done
curl -fsS http://127.0.0.1:18080/ | grep -c "Mappings"
docker exec ytjs-smoke sh -c 'deno --version && ffmpeg -version | head -1 && python -c "import yt_dlp.version as v; print(v.__version__)"'
docker logs ytjs-smoke | tail -20
docker stop ytjs-smoke
```
Expected:
- `/healthz` returns `{"ok":true}`.
- The grep count is ≥ 1.
- deno, ffmpeg and yt-dlp versions print.
- The logs show `yt-jellyfin-sync started` and a `Checking for yt-dlp updates` line.
- `docker stop` returns within a few seconds (the signal is forwarded).

- [ ] **Step 8: Manual end-to-end check in a browser** (with the container running again, as in Step 7)

1. Create a **Show** mapping with folder `shows/Test` and a short public playlist URL. The card shows "Checking playlist…" and then "N new video(s)". The activity panel shows a live progress bar for the first download, then "next download in …" counting down.
2. On the mapping page, drag the second video above the first. The episode codes and file name previews swap. On disk (`/tmp/ytjs-smoke/media/shows/Test/Season 01`), the downloaded files are renamed to match.
3. Click "New season here" on a video. A "Season 2" header appears and the file moves to `Season 02/`.
4. Rename a video. The file and `.nfo` title change.
5. Delete a video. It appears under "Ignored videos", and "Sync now" does not bring it back.
6. Create a **Music videos** manual mapping and paste a music video URL. It lands as `Artist - Song.mkv`.
7. Open **Logs**. New lines stream in live.

- [ ] **Step 9: Commit**

```bash
git add Dockerfile .dockerignore docker docker-compose.yml README.md tests/test_integration.py
git commit -m "feat: docker image with restart loop, compose example and README"
```
