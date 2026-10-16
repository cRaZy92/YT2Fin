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
