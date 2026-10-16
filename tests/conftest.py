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
        self.hold_until = None

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
