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
