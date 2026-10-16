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


def _normalize(version: str) -> tuple:
    """yt-dlp reports '2026.08.19' while package metadata says '2026.8.19' (PEP 440)."""
    return tuple(int(part) if part.isdigit() else part for part in version.strip().split("."))


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
            if _normalize(installed) != _normalize(running):
                self.last_result = f"Installed yt-dlp {installed}; restarting when idle"
                self.worker.restart_pending = True
                self.worker.kick()
            else:
                self.last_result = f"Up to date ({running})"
            log.info(self.last_result)
            return self.last_result
