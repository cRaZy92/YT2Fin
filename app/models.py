from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, String, Text, UniqueConstraint, false
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
    DELETED = "deleted"  # removed by the user: no files, no number, sync skips it; can be restored


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
    include_shorts: Mapped[bool] = mapped_column(default=False, server_default=false())
    reverse_order: Mapped[bool] = mapped_column(default=False, server_default=false())  # newest-first playlists
    title_filter: Mapped[str | None] = mapped_column(String(300), default=None)
    description_mode: Mapped[str] = mapped_column(String(20), default="cleaned", server_default="cleaned")
    description_remove: Mapped[str | None] = mapped_column(Text, default=None)  # extra "remove lines containing"
    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    videos: Mapped[list[Video]] = relationship(
        back_populates="mapping", order_by="Video.position", cascade="all, delete-orphan"
    )
    season_breaks: Mapped[list[SeasonBreak]] = relationship(cascade="all, delete-orphan")


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
    original_position: Mapped[int | None] = mapped_column(default=None)  # order when added; never changes
    custom_title: Mapped[str | None] = mapped_column(String(500), default=None)
    artist: Mapped[str | None] = mapped_column(String(300), default=None)
    song: Mapped[str | None] = mapped_column(String(500), default=None)
    user_edited: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(20), default=VideoStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    attempts: Mapped[int] = mapped_column(default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(default=None)
    file_path: Mapped[str | None] = mapped_column(String(1000), default=None)
    file_size: Mapped[int | None] = mapped_column(BigInteger, default=None)  # bytes of the media file
    downloaded_at: Mapped[datetime | None] = mapped_column(default=None)
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


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
