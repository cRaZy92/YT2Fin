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
