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
