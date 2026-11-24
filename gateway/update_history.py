#!/usr/bin/env python3
"""When each software version was installed on the Pi (read-only for the control).

Two sources, merged when they are read:

* ``update-history.json`` (written by the gateway): at each start the gateway
  compares its VERSION with the last recorded one and appends a new entry when it
  differs. The time is the VERSION file's modification time, which update.sh sets
  when it installs the file, so it is the install time, not the start time.
* ``update-history-backfill.json`` (written by update.sh as root): every backup
  ``/var/backups/hch5-control-update-*`` holds the build that was replaced, and its
  ``./VERSION`` keeps the time that build was installed. So each backup gives one
  exact (version, install time) pair, also for the updates before this file
  existed. Only that member is read (it is near the start of the archive), the
  newest backups first, within a time budget; backups already read are skipped.

Nothing here touches the regulation, the settings or the hardware.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import tarfile
import tempfile
import time
from pathlib import Path

UPDATE_HISTORY_FILE = "update-history.json"
UPDATE_BACKFILL_FILE = "update-history-backfill.json"
MAX_ENTRIES = 50
# update.sh reads backups this old at most (the history keeps 30 days, a window 7 more).
BACKFILL_DAYS = 45
BACKFILL_BUDGET_S = 20.0
BACKUP_PATTERN = re.compile(r"^hch5-control-update-(\d{8}-\d{6})$")
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}$")


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value > 0 else None


def _clean_version(value: object) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text if VERSION_PATTERN.match(text) else None


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _entries(data: dict) -> list[dict]:
    items = data.get("entries") if isinstance(data.get("entries"), list) else []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        version, at = _clean_version(item.get("version")), _number(item.get("at"))
        if version and at:
            result.append({"version": version, "at": at})
    return result


def find_version_file(start: Path | None = None) -> Path | None:
    """VERSION next to the gateway files (the Pi) or one level up (the repository)."""
    here = (start or Path(__file__)).resolve().parent
    for candidate in (here / "VERSION", here.parent / "VERSION"):
        if candidate.is_file():
            return candidate
    return None


def record_start(path: str | Path, version_file: str | Path | None, now: float | None = None) -> dict | None:
    """Append the running version when it differs from the last recorded one.

    Returns the new entry, or None when nothing changed (or nothing is known)."""
    now = time.time() if now is None else now
    if version_file is None:
        return None
    version_file = Path(version_file)
    try:
        version = _clean_version(version_file.read_text(encoding="utf-8"))
        installed = version_file.stat().st_mtime
    except OSError:
        return None
    if version is None:
        return None
    path = Path(path)
    entries = _entries(_read_json(path))
    last = max(entries, key=lambda item: item["at"]) if entries else None
    if last is not None and last["version"] == version:
        return None
    # The install time from the file, unless the clock or the file says otherwise.
    at = installed if installed <= now + 60 and (last is None or installed > last["at"]) else now
    entry = {"version": version, "at": round(at, 1)}
    entries.append(entry)
    entries.sort(key=lambda item: item["at"])
    _write_json(path, {"entries": entries[-MAX_ENTRIES:]})
    return entry


def merged_updates(state_dir: str | Path) -> list[dict]:
    """All known installs, oldest first, one entry per version change.

    Each entry: ``id`` (the install time as a whole second, as text), ``version``,
    ``previous_version`` (None for the oldest known), ``at`` and ``source``
    ("start" = recorded by the gateway, "backup" = read from an update backup)."""
    state_dir = Path(state_dir)
    items = [{**item, "source": "start"} for item in _entries(_read_json(state_dir / UPDATE_HISTORY_FILE))]
    items += [{**item, "source": "backup"} for item in _entries(_read_json(state_dir / UPDATE_BACKFILL_FILE))]
    items.sort(key=lambda item: (item["at"], item["source"] != "backup"))
    merged: list[dict] = []
    for item in items:
        if merged and merged[-1]["version"] == item["version"]:
            # The same install seen twice, or the same version installed again
            # (e.g. after a rollback): the version did not change, so no new entry.
            continue
        merged.append(dict(item))
    result = [{"id": str(int(item["at"])), "version": item["version"],
               "previous_version": merged[index - 1]["version"] if index else None,
               "at": round(item["at"]), "source": item["source"]} for index, item in enumerate(merged)]
    return result[-MAX_ENTRIES:]


def _backup_version(archive: Path) -> tuple[str, float] | None:
    """The replaced build's VERSION and its install time, from the start of the archive."""
    with tarfile.open(archive, "r|gz") as tar:
        for index, member in enumerate(tar):
            if member.name in ("./VERSION", "VERSION") and member.isfile() and member.size < 100:
                handle = tar.extractfile(member)
                version = _clean_version(handle.read().decode("utf-8", "replace")) if handle else None
                return (version, float(member.mtime)) if version and member.mtime > 0 else None
            if index > 2000:
                return None
    return None


def backfill(backup_root: str | Path, out_path: str | Path, now: float | None = None,
             budget_s: float = BACKFILL_BUDGET_S, days: int = BACKFILL_DAYS) -> dict:
    """Read the version of each recent update backup (newest first) into ``out_path``."""
    now = time.time() if now is None else now
    out_path = Path(out_path)
    saved = _read_json(out_path)
    scanned = saved.get("scanned") if isinstance(saved.get("scanned"), dict) else {}
    found: dict[str, list] = {key: value for key, value in scanned.items() if isinstance(key, str)}
    started = time.monotonic()
    candidates = []
    try:
        names = os.listdir(backup_root)
    except OSError:
        names = []
    for name in names:
        match = BACKUP_PATTERN.match(name)
        if not match or name in found:
            continue
        try:
            stamp = time.mktime(time.strptime(match.group(1), "%Y%m%d-%H%M%S"))
        except ValueError:
            continue
        if now - stamp <= days * 86400:
            candidates.append((stamp, name))
    read = 0
    for _stamp, name in sorted(candidates, reverse=True):
        if read and time.monotonic() - started > budget_s:
            break
        try:
            pair = _backup_version(Path(backup_root) / name / "app-code.tar.gz")
        except (OSError, tarfile.TarError, EOFError):
            pair = None
        found[name] = [pair[0], round(pair[1], 1)] if pair else None
        read += 1
    entries = {}
    for value in found.values():
        if isinstance(value, list) and len(value) == 2 and _clean_version(value[0]) and _number(value[1]):
            entries[(value[0], value[1])] = {"version": value[0], "at": value[1]}
    result = {"scanned": found, "entries": sorted(entries.values(), key=lambda item: item["at"])}
    _write_json(out_path, result)
    return {"read": read, "left": len(candidates) - read, "entries": len(result["entries"])}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "--backfill":
        print("usage: update_history.py --backfill BACKUP_DIR STATE_DIR", file=sys.stderr)
        return 2
    summary = backfill(argv[1], Path(argv[2]) / UPDATE_BACKFILL_FILE)
    print(f"Update history: read {summary['read']} backups, {summary['left']} left, {summary['entries']} versions known.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
