"""The Pi itself: disk, undervoltage and whether the clock is synchronised.

Read at most once a minute (two short system calls with a timeout) and cached, so the diagnostics
can raise an alarm without a call on every pass. A value that cannot be read is None, never an
alarm: a missing tool (vcgencmd on a PC, timedatectl in a container) says nothing about the Pi.
"""
from __future__ import annotations

import shutil
import subprocess
import time

# vcgencmd get_throttled: bit 0 is "under-voltage now".
UNDERVOLTAGE_NOW = 0x1
READ_EVERY_SECONDS = 60.0


def _run(*args: str) -> str | None:
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=2, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


class PiHealth:
    def __init__(self, interval: float = READ_EVERY_SECONDS, runner=_run, disk_path: str = "/") -> None:
        self.interval = interval
        self.runner = runner
        self.disk_path = disk_path
        self.read_at: float | None = None
        self.values: dict[str, object] = {}

    def read(self, now: float | None = None) -> dict[str, object]:
        now = time.monotonic() if now is None else now
        if self.read_at is not None and 0 <= now - self.read_at < self.interval:
            return dict(self.values)
        self.read_at = now
        values: dict[str, object] = {"pi_disk_used_percent": None, "pi_undervoltage_now": None, "pi_clock_synced": None}
        try:
            usage = shutil.disk_usage(self.disk_path)
            values["pi_disk_used_percent"] = round(usage.used / usage.total * 100, 1) if usage.total else None
        except OSError:
            pass
        throttled = self.runner("/usr/bin/vcgencmd", "get_throttled")
        if throttled and "=" in throttled:
            try:
                values["pi_undervoltage_now"] = bool(int(throttled.partition("=")[2], 16) & UNDERVOLTAGE_NOW)
            except ValueError:
                pass
        synced = self.runner("timedatectl", "show", "-p", "NTPSynchronized", "--value")
        if synced in ("yes", "no"):
            values["pi_clock_synced"] = synced == "yes"
        self.values = values
        return dict(values)
