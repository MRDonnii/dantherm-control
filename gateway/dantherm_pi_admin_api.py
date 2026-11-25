#!/usr/bin/env python3
"""Authenticated, allowlisted privileged actions for HCH5 Control."""
from __future__ import annotations

import hmac
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from diagnostics_report import build_report

TOKEN = os.environ["DANTHERM_REBOOT_TOKEN"]
BIND = os.getenv("DANTHERM_ADMIN_BIND", "127.0.0.1")
PORT = int(os.getenv("DANTHERM_ADMIN_PORT", "4198"))
PROFILE_FILE = Path("/var/lib/dantherm-admin/power-profile")
UPDATE_CHANNEL_FILE = Path("/var/lib/dantherm-admin/update-channel")
# The last update attempt survives a restart of this helper, and the output of
# update.sh is kept so a failure can be explained afterwards.
UPDATE_STATE_FILE = Path(os.getenv("DANTHERM_UPDATE_STATE_FILE", "/var/lib/dantherm-admin/update-state.json"))
UPDATE_LOG_FILE = Path(os.getenv("DANTHERM_UPDATE_LOG_FILE", "/var/lib/dantherm-admin/update-last.log"))
UPDATE_LOG_MAX_BYTES = 200 * 1024
UPDATE_LOG_TAIL_LINES = 40
FILTER_STATE_FILE = Path("/var/lib/dantherm-hch5-ha/filter-state.json")
APP_DIR = Path("/opt/dantherm-passivelink-webui")
VERSION_FILE = APP_DIR / "VERSION"
BUILD_FILE = APP_DIR / "BUILD"
REPOSITORY = "MRDonnii/dantherm-control"
BETA_FEED = f"https://github.com/{REPOSITORY}/releases.atom"
USER_AGENT = "HCH5-Control-Updater/1.2"
PROFILES = {"powersave": "powersave", "balanced": "ondemand", "performance": "performance"}
SERVICES = {
    "gateway": os.getenv("DANTHERM_GATEWAY_SERVICE", "dantherm-webui-gateway.service"),
    "onewire": os.getenv("DANTHERM_ONEWIRE_SERVICE", "dantherm-webui-onewire.service"),
}
ADMIN_SERVICE = os.getenv("DANTHERM_ADMIN_SERVICE", "dantherm-webui-admin.service")
REPORT_SERVICES = {**SERVICES, "admin": ADMIN_SERVICE}
DIAGNOSTICS_LOCK = threading.Lock()
UPDATE_LOCK = threading.Lock()
UPDATE_STATE = {
    "running": False,
    "channel": None,
    "started_at": None,
    "finished_at": None,
    "progress": 0,
    "phase": "idle",
    "detail": "Klar",
    "last_error": None,
    # The last attempt: when, from which version to which, on which channel,
    # how it ended, and the end of the update.sh output when it failed.
    "from_version": None,
    "target_version": None,
    "ref": None,
    "result": None,
    "log_tail": None,
}
PERSISTED_UPDATE_KEYS = ("channel", "started_at", "finished_at", "progress", "phase", "detail", "last_error",
                         "from_version", "target_version", "ref", "result", "log_tail")
FILTER_INTERVAL_MIN_DAYS = 90
FILTER_INTERVAL_MAX_DAYS = 360


def _set_update_state(**changes) -> None:
    with UPDATE_LOCK:
        UPDATE_STATE.update(changes)
        snapshot = {key: UPDATE_STATE.get(key) for key in PERSISTED_UPDATE_KEYS}
    try:
        _atomic_write_text(UPDATE_STATE_FILE, json.dumps(snapshot, separators=(",", ":")) + "\n")
    except OSError:
        pass


def load_update_state() -> None:
    """Restore the last attempt after a restart of this helper."""
    try:
        saved = json.loads(UPDATE_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict):
        return
    restored = {key: saved.get(key) for key in PERSISTED_UPDATE_KEYS if key in saved}
    if restored.get("phase") not in {None, "idle", "complete", "failed", "up_to_date", "dismissed", "deferred"}:
        # The helper stopped in the middle of an attempt (update.sh restarts it
        # only after a successful update, so this is an interrupted one).
        restored.update(phase="failed", result="failed", detail="Opdateringen blev afbrudt, før den blev færdig.",
                        last_error=restored.get("last_error") or "Afbrudt: admin-tjenesten genstartede under opdateringen.")
    elif restored.get("phase") == "complete":
        restored.update(detail=f"Opdateret til {restored.get('target_version') or 'ny version'}.")
    with UPDATE_LOCK:
        UPDATE_STATE.update(restored, running=False)


def _clear_update_error(reason: str) -> None:
    """Hide a finished failure; the attempt itself stays on record."""
    _set_update_state(phase="dismissed", progress=0, detail=reason, last_error=None, log_tail=None)


def update_state_view() -> dict[str, object]:
    """The update state for the WebUI. A failure is cleared by itself once the
    installed version is the attempt's target or newer, e.g. after a later
    update from the WebUI or a manual update.sh succeeded."""
    with UPDATE_LOCK:
        state = dict(UPDATE_STATE)
    if state.get("phase") == "failed" and not state.get("running"):
        current, target = version_key(current_version()), version_key(str(state.get("target_version") or ""))
        if current is not None and target is not None and current >= target:
            _clear_update_error(f"Version {current_version()} er installeret; den gamle fejl er ryddet.")
            with UPDATE_LOCK:
                state = dict(UPDATE_STATE)
    return state


def _tail_lines(path: Path, lines: int = UPDATE_LOG_TAIL_LINES) -> str | None:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 16 * 1024))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    return "\n".join(text.splitlines()[-lines:]) or None


def _open_update_log():
    """Start a fresh log for this attempt; the previous one becomes .1."""
    UPDATE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if UPDATE_LOG_FILE.exists():
        UPDATE_LOG_FILE.replace(UPDATE_LOG_FILE.with_name(UPDATE_LOG_FILE.name + ".1"))
    return UPDATE_LOG_FILE.open("w", encoding="utf-8")


def _cap_update_log() -> None:
    """Keep only the newest UPDATE_LOG_MAX_BYTES of the log."""
    try:
        if UPDATE_LOG_FILE.stat().st_size <= UPDATE_LOG_MAX_BYTES:
            return
        with UPDATE_LOG_FILE.open("rb") as handle:
            handle.seek(-UPDATE_LOG_MAX_BYTES, os.SEEK_END)
            data = handle.read()
        data = data[data.find(b"\n") + 1:] if b"\n" in data else data
        _atomic_write_bytes(UPDATE_LOG_FILE, "[... ældre linjer fjernet ...]\n".encode() + data)
    except OSError:
        pass


def set_profile(profile):
    governor = PROFILES[profile]
    paths = tuple(Path("/sys/devices/system/cpu/cpufreq").glob("policy[0-9]*/scaling_governor"))
    if not paths or any(governor not in (path.parent / "scaling_available_governors").read_text().split() for path in paths):
        raise ValueError("profile_unavailable")
    for path in paths:
        path.write_text(governor)
    PROFILE_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_FILE.write_text(profile + "\n")


def _nmcli(*args: str, timeout: int = 15) -> str:
    result = subprocess.run(["/usr/bin/nmcli", *args], capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("network_manager_unavailable" if result.returncode == 127 else "wifi_command_failed")
    return result.stdout.strip()


def _nmcli_fields(line: str) -> list[str]:
    fields, part, escaped = [], "", False
    for char in line:
        if escaped:
            part += char; escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append(part); part = ""
        else:
            part += char
    fields.append(part)
    return fields


def system_status() -> dict[str, object]:
    governor = Path("/sys/devices/system/cpu/cpufreq/policy0/scaling_governor").read_text().strip()
    available = (Path("/sys/devices/system/cpu/cpufreq/policy0/scaling_available_governors").read_text().split())
    result: dict[str, object] = {
        "power_profile": next((p for p, g in PROFILES.items() if g == governor), governor),
        "available_profiles": [p for p, g in PROFILES.items() if g in available],
        "governor": governor,
        "network_manager_available": shutil.which("nmcli") is not None,
        "wifi_available": False,
        "wifi_enabled": False,
        "wifi_connection": None,
        "wifi_ipv4": None,
        "bluetooth_available": Path("/sys/class/bluetooth/hci0").exists(),
    }
    try:
        devices = _nmcli("-t", "--escape", "yes", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
        wifi = next((_nmcli_fields(line) for line in devices.splitlines() if len(_nmcli_fields(line)) >= 4 and _nmcli_fields(line)[1] == "wifi"), None)
        result["wifi_available"] = bool(wifi)
        result["wifi_enabled"] = _nmcli("-g", "WIFI", "general") == "enabled"
        if wifi:
            if wifi[2] == "connected":
                connection = wifi[3]
                try:
                    result["wifi_connection"] = _nmcli("-g", "802-11-wireless.ssid", "connection", "show", "id", connection) or connection
                except RuntimeError:
                    result["wifi_connection"] = connection
            address = _nmcli("-g", "IP4.ADDRESS", "device", "show", wifi[0])
            result["wifi_ipv4"] = address.splitlines()[0].split("/")[0] if address else None
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        pass
    return result


def wifi_scan() -> list[dict[str, object]]:
    if not system_status()["wifi_available"]:
        return []
    rows = _nmcli("-t", "--escape", "yes", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", "wlan0", "--rescan", "yes", timeout=20)
    networks: dict[str, dict[str, object]] = {}
    for row in rows.splitlines():
        fields = _nmcli_fields(row)
        if len(fields) != 4 or not fields[1]:
            continue
        ssid = fields[1]
        try: signal = max(0, min(100, int(fields[2])))
        except ValueError: signal = 0
        candidate = {"ssid": ssid, "signal": signal, "security": fields[3], "connected": fields[0] == "*"}
        if ssid not in networks or signal > int(networks[ssid]["signal"]):
            networks[ssid] = candidate
    return sorted(networks.values(), key=lambda item: (not item["connected"], -int(item["signal"]), str(item["ssid"]).lower()))[:40]


def wifi_connect(target: object) -> dict[str, object]:
    if not isinstance(target, dict):
        raise ValueError("invalid_wifi_request")
    ssid, password = target.get("ssid"), target.get("password")
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or any(ord(ch) < 32 for ch in ssid):
        raise ValueError("invalid_ssid")
    if not isinstance(password, str) or not 8 <= len(password) <= 63 or any(ord(ch) < 32 for ch in password):
        raise ValueError("invalid_wifi_password")
    if not system_status()["wifi_available"]:
        raise RuntimeError("wifi_radio_unavailable")
    profile = "hch5-wifi-" + hashlib.sha256(ssid.encode()).hexdigest()[:8] + "-" + secrets.token_hex(2)
    _nmcli("connection", "add", "type", "wifi", "ifname", "wlan0", "con-name", profile, "ssid", ssid,
           "wifi-sec.key-mgmt", "wpa-psk", "connection.autoconnect", "yes", "ipv4.route-metric", "600", "ipv6.route-metric", "600")
    credential_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=PROFILE_FILE.parent, prefix="wifi-pass-", delete=False) as handle:
            credential_path = Path(handle.name)
            os.chmod(credential_path, 0o600)
            handle.write("802-11-wireless-security.psk:" + password + "\n")
        _nmcli("--wait", "35", "connection", "up", "id", profile, "ifname", "wlan0", "passwd-file", str(credential_path), timeout=40)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        subprocess.run(["/usr/bin/nmcli", "connection", "delete", "id", profile], capture_output=True, timeout=10, check=False)
        raise RuntimeError("wifi_connection_failed") from None
    finally:
        if credential_path is not None:
            credential_path.unlink(missing_ok=True)
    # The live NFS boot uses a RAM-backed NetworkManager profile directory.
    # Keep a private durable copy; ordinary SD-card installs still use NM's
    # normal persistent directory and do not depend on this copy.
    saved_dir = PROFILE_FILE.parent / "nm-connections"
    for source in Path("/etc/NetworkManager/system-connections").glob("hch5-wifi-*.nmconnection"):
        saved_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(saved_dir, 0o700)
        destination = saved_dir / source.name
        shutil.copy2(source, destination)
        os.chmod(destination, 0o600)
    return system_status()


def _request_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read(128 * 1024).decode("utf-8", "replace").strip()


def _final_url(url: str) -> str:
    """Resolve a public GitHub redirect without consuming REST API quota."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.geturl()


def version_key(value: str) -> tuple[int, int, int, int, int] | None:
    """Sortable key: 1.3.0 > 1.3.0-beta.9 > 1.2.1-beta.13. None if unknown."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?", str(value).strip())
    if not match:
        return None
    major, minor, patch, beta = match.groups()
    return (int(major), int(minor), int(patch), 0 if beta is not None else 1, int(beta or 0))


def is_newer(remote: str, current: str) -> bool:
    remote_key, current_key = version_key(remote), version_key(current)
    if remote_key is None or current_key is None:
        return remote != current
    return remote_key > current_key


def is_ahead_of_release(current: str, released: str) -> bool:
    """True when the installed version is newer than the newest release.

    That happens when a fix is installed from main before it is published,
    e.g. 1.6.1 running while the stable channel still points at 1.6.0. Unknown
    version formats never count as ahead, so the UI falls back to its normal
    text instead of claiming something it cannot know.
    """
    current_key, released_key = version_key(current), version_key(released)
    if current_key is None or released_key is None:
        return False
    return current_key > released_key


def _latest_beta_tag() -> str:
    """Newest release for the beta channel from GitHub's public Atom feed.

    The feed lists stable releases too; a stable release newer than every
    beta (for example 1.3.0 after 1.2.1-beta.13) is what the beta channel
    should get, so both kinds are compared by version.
    """
    feed = _request_text(BETA_FEED)
    tags = set(re.findall(r"v\d+\.\d+\.\d+(?:-beta\.\d+)?(?![\w.-])", feed))
    if not tags:
        raise ValueError("beta_release_missing_from_feed")
    return max(tags, key=lambda tag: version_key(tag) or (0, 0, 0, 0, 0))


def _read_text(path: Path, fallback: str = "unknown") -> str:
    try:
        return path.read_text(encoding="utf-8").strip() or fallback
    except OSError:
        return fallback


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def current_version() -> str:
    return _read_text(VERSION_FILE)


def current_build() -> str:
    return _read_text(BUILD_FILE)


def current_update_channel() -> str:
    value = _read_text(UPDATE_CHANNEL_FILE, "stable").lower()
    return value if value in {"stable", "beta"} else "stable"


def set_update_channel(channel: str) -> str:
    if channel not in {"stable", "beta"}:
        raise ValueError("invalid_channel")
    _atomic_write_text(UPDATE_CHANNEL_FILE, channel + "\n")
    return channel


def _read_filter_state() -> dict[str, object]:
    reset_epoch = time.time()
    interval_days = FILTER_INTERVAL_MAX_DAYS
    try:
        saved = json.loads(FILTER_STATE_FILE.read_text(encoding="utf-8"))
        reset_epoch = float(saved.get("reset_epoch", reset_epoch))
        interval_days = int(saved.get("interval_days", interval_days))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    interval_days = max(FILTER_INTERVAL_MIN_DAYS, min(FILTER_INTERVAL_MAX_DAYS, interval_days))
    elapsed = max(0.0, (time.time() - reset_epoch) / 86400.0)
    remaining = max(0, math.ceil(interval_days - elapsed))
    percent = max(0, min(100, round(remaining / interval_days * 100)))
    return {
        "reset_epoch": reset_epoch,
        "interval_days": interval_days,
        "days_remaining": remaining,
        "life_percent": percent,
    }


def _write_filter_state(*, interval_days: int | None = None, reset_now: bool = False) -> dict[str, object]:
    state = _read_filter_state()
    if interval_days is not None:
        interval_days = int(interval_days)
        if not FILTER_INTERVAL_MIN_DAYS <= interval_days <= FILTER_INTERVAL_MAX_DAYS:
            raise ValueError("invalid_filter_interval")
        state["interval_days"] = interval_days
    if reset_now:
        state["reset_epoch"] = time.time()
    payload = {
        "reset_epoch": float(state["reset_epoch"]),
        "interval_days": int(state["interval_days"]),
    }
    _atomic_write_text(FILTER_STATE_FILE, json.dumps(payload, separators=(",", ":")) + "\n")
    return _read_filter_state()


def _schedule_service_restart(service: str | list[str], delay: float = 0.7) -> None:
    units = [service] if isinstance(service, str) else list(service)

    def restart():
        time.sleep(delay)
        subprocess.run(["systemctl", "restart", *units], check=False)
    threading.Thread(target=restart, daemon=True).start()


_UPDATE_INFO_CACHE: dict[str, dict[str, object]] = {}
_UPDATE_INFO_CACHE_AT: dict[str, float] = {}
UPDATE_INFO_CACHE_SECONDS = 300


def update_info(channel: str | None = None, *, force_refresh: bool = False) -> dict[str, object]:
    channel = channel or current_update_channel()
    if channel not in {"stable", "beta"}:
        raise ValueError("invalid_channel")
    now = time.monotonic()
    cached = _UPDATE_INFO_CACHE.get(channel)
    cache_age = now - _UPDATE_INFO_CACHE_AT.get(channel, 0.0)
    if not force_refresh and cached is not None and cache_age < UPDATE_INFO_CACHE_SECONDS:
        return {**cached, "current_build": current_build(), "update": update_state_view()}
    try:
        current = current_version()
        installed_build = current_build()
        available_build = None
        if channel == "stable":
            latest_url = _final_url(f"https://github.com/{REPOSITORY}/releases/latest")
            ref = urllib.parse.unquote(urllib.parse.urlparse(latest_url).path.rsplit("/", 1)[-1])
            if not ref or ref == "latest":
                raise ValueError("latest_release_redirect_missing_tag")
            remote_version = ref.lstrip("v")
            published = None
            update_available = is_newer(remote_version, current)
        else:
            ref = _latest_beta_tag()
            remote_version = ref.lstrip("v")
            available_build = remote_version
            published = None
            update_available = is_newer(remote_version, current)
    except (OSError, ValueError, KeyError, urllib.error.URLError, json.JSONDecodeError) as error:
        # A GitHub rate limit (HTTP 403) or transient network error should not
        # spam the UI with a failure on every poll. Fall back to the last
        # known-good check instead of raising, if one exists.
        if cached is not None:
            return {**cached, "current_build": current_build(), "update": update_state_view(), "check_error": str(error)}
        raise
    result = {
        "ok": True,
        "channel": channel,
        "current_version": current,
        "current_build": installed_build,
        "available_version": remote_version,
        "available_build": available_build,
        "ref": ref,
        "published_at": published,
        "update_available": update_available,
        "ahead_of_release": is_ahead_of_release(current, remote_version),
    }
    _UPDATE_INFO_CACHE[channel] = result
    _UPDATE_INFO_CACHE_AT[channel] = now
    return {**result, "update": update_state_view()}


# update.sh: exit code when the frost lock defers an update that was not forced.
UPDATE_DEFERRED_FROST_RC = 3
# update.sh normally ends within ~4 min plus its soak; past this it is asked to
# stop (SIGTERM, so its trap rolls back) and given time to finish the rollback.
UPDATE_TIMEOUT_SECONDS = 900
UPDATE_ROLLBACK_GRACE_SECONDS = 300
CONTROLLER_STATE_FILE = Path(os.getenv("DANTHERM_CONTROLLER_STATE_FILE", "/var/lib/dantherm-hch5-ha/controller.json"))
FROST_DEFER_MESSAGE = ("Det fryser, og eftervarmens frostsikring er slået til. Opdateringen venter derfor, "
                       "til det er tøet. Du kan vælge at opdatere alligevel.")


def frost_lock_latched() -> bool:
    try:
        return json.loads(CONTROLLER_STATE_FILE.read_text(encoding="utf-8")).get("frost_lock_latched") is True
    except (OSError, ValueError, AttributeError):
        return False


def claim_update() -> bool:
    """Mark an update as running before the request is answered (no double start)."""
    with UPDATE_LOCK:
        if UPDATE_STATE.get("running"):
            return False
        UPDATE_STATE["running"] = True
        return True


def release_update_claim() -> None:
    with UPDATE_LOCK:
        UPDATE_STATE["running"] = False


def _run_updater(command: list[str], *, cwd: Path, env: dict, log) -> int:
    """Run update.sh; on a hang stop it with SIGTERM so its own rollback runs.

    subprocess.run(timeout=...) sends SIGKILL, which skipped the rollback.
    """
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        return process.wait(timeout=UPDATE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=UPDATE_ROLLBACK_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise RuntimeError(f"update.sh svarede ikke inden for {UPDATE_TIMEOUT_SECONDS // 60} minutter og blev stoppet")


def _release_checksums(ref: str) -> dict[str, str] | None:
    """The release's SHA256SUMS asset (sha256 per file in the source tree), if it has one."""
    url = f"https://github.com/{REPOSITORY}/releases/download/{urllib.parse.quote(ref, safe='')}/SHA256SUMS"
    try:
        text = _request_text(url)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *]?(.+)", line.strip())
        if match:
            sums[match.group(2).lstrip("./")] = match.group(1)
    return sums or None


def verify_source_tree(source: Path, sums: dict[str, str]) -> int:
    """Every file in the downloaded tree must be listed with the same SHA-256."""
    seen = 0
    for path in sorted(p for p in source.rglob("*") if p.is_file() or p.is_symlink()):
        relative = path.relative_to(source).as_posix()
        expected = sums.get(relative)
        if expected is None:
            raise RuntimeError(f"Kontrolsum mangler for {relative}")
        if path.is_symlink():
            raise RuntimeError(f"Uventet link i udgivelsen: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise RuntimeError(f"Kontrolsummen passer ikke for {relative}")
        seen += 1
    missing = sorted(set(sums) - {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()})
    if missing:
        raise RuntimeError(f"Filen {missing[0]} mangler i udgivelsen")
    return seen


def _download_tarball(ref: str, destination: Path) -> None:
    request = urllib.request.Request(
        f"https://codeload.github.com/{REPOSITORY}/tar.gz/{urllib.parse.quote(ref, safe='')}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=45) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)


def _safe_extract(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        roots = {Path(member.name).parts[0] for member in members if member.name and Path(member.name).parts}
        if len(roots) != 1:
            raise RuntimeError("unexpected_archive_layout")
        root = next(iter(roots))
        base = destination.resolve()
        for member in members:
            target = (destination / member.name).resolve()
            if base not in target.parents and target != base:
                raise RuntimeError("unsafe_archive_path")
        tar.extractall(destination)
    return destination / root


def _schedule_admin_restart() -> None:
    unit = f"hch5-control-admin-restart-{int(time.time())}"
    subprocess.run(
        [
            "systemd-run", "--quiet", f"--unit={unit}", "--on-active=2s",
            "/usr/bin/systemctl", "restart", ADMIN_SERVICE,
        ],
        check=False,
        timeout=10,
    )


def _install_update(channel: str, force: bool = False) -> None:
    _set_update_state(
        running=True,
        channel=channel,
        started_at=time.time(),
        finished_at=None,
        progress=2,
        phase="starting",
        detail="Forbereder sikker opdatering…",
        last_error=None,
        from_version=current_version(),
        target_version=None,
        ref=None,
        result=None,
        log_tail=None,
    )
    success = False
    log = None
    try:
        log = _open_update_log()
        log.write(f"HCH5 Control update {time.strftime('%Y-%m-%d %H:%M:%S')} · kanal {channel} · installeret {current_version()}\n")
        log.flush()
        set_update_channel(channel)
        _set_update_state(progress=7, phase="checking", detail="Kontrollerer kanal og tilgængelig build…")
        info = update_info(channel, force_refresh=True)
        ref = str(info["ref"])
        target = str(info.get("available_version") or ref.lstrip("v"))
        build = str(info.get("available_build") or ref)
        _set_update_state(target_version=target, ref=ref)
        log.write(f"Mål: {target} (ref {ref})\n")
        # Never install a build that is not newer than the running one, whatever
        # the channel points at (an old prerelease, or main ahead of releases).
        if not is_newer(target, current_version()):
            log.write("Ingen nyere build; intet installeret.\n")
            _set_update_state(progress=0, phase="up_to_date", result="up_to_date",
                              detail=f"Ingen nyere build: {current_version()} kører, kanalen tilbyder {target}. Intet installeret.")
            return
        _set_update_state(progress=14, phase="preparing", detail="Opretter isoleret staging-område…")
        with tempfile.TemporaryDirectory(prefix="hch5-control-update-") as temporary:
            directory = Path(temporary)
            archive = directory / "source.tar.gz"
            _set_update_state(progress=24, phase="downloading", detail="Downloader kildekode…")
            _download_tarball(ref, archive)
            _set_update_state(progress=38, phase="extracting", detail="Pakker ny build ud…")
            source = _safe_extract(archive, directory / "src")
            sums = _release_checksums(ref)
            if sums is None:
                log.write("Ingen SHA256SUMS i udgivelsen; filerne er ikke kontrolleret mod en kontrolsum.\n")
            else:
                _set_update_state(progress=44, phase="verifying_download", detail="Kontrollerer filerne mod udgivelsens kontrolsummer…")
                log.write(f"Kontrolsummer OK for {verify_source_tree(source, sums)} filer.\n")
            updater = source / "update.sh"
            if not updater.is_file():
                raise RuntimeError("updater_missing")
            _set_update_state(progress=50, phase="validating", detail="Validerer updater og eksisterende installation…")
            env = os.environ.copy()
            env["HCH5_UPDATE_BUILD"] = build
            # update.sh must not restart this helper while it is its parent.
            env["HCH5_UPDATE_FROM_ADMIN"] = "1"
            if force:
                env["HCH5_UPDATE_FORCE"] = "1"
                log.write("Opdatering under frost valgt alligevel.\n")
            _set_update_state(progress=58, phase="installing", detail="Installerer og kører failsafe-validering…")
            log.write(f"$ bash {updater.relative_to(directory)}\n")
            log.flush()
            returncode = _run_updater(["bash", str(updater)], cwd=source, env=env, log=log)
            if returncode == UPDATE_DEFERRED_FROST_RC:
                _set_update_state(progress=0, phase="deferred", result="deferred_frost", detail=FROST_DEFER_MESSAGE)
                return
            if returncode != 0:
                raise RuntimeError(f"update.sh stoppede med kode {returncode}")
            _set_update_state(progress=93, phase="verifying", detail="Bekræfter ny build og services…")
        success = True
        _set_update_state(progress=100, phase="complete", result="complete", detail="Opdatering installeret. Genindlæser WebUI…")
    except Exception as error:
        message = str(error) if type(error) is RuntimeError else f"{type(error).__name__}: {error}"
        if log is not None:
            try:
                log.write(f"FEJL: {message}\n")
                log.flush()
            except (OSError, ValueError):
                pass
        _set_update_state(
            phase="failed",
            result="failed",
            detail="Opdateringen fejlede og failsafe har bevaret/tilbageført installationen.",
            last_error=message,
        )
    finally:
        if log is not None:
            log.close()
            _cap_update_log()
            if UPDATE_STATE.get("phase") == "failed":
                _set_update_state(log_tail=_tail_lines(UPDATE_LOG_FILE))
        _set_update_state(running=False, finished_at=time.time())
    if success:
        _schedule_admin_restart()


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}")

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True})
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        if self.path == "/diagnostics":
            if not DIAGNOSTICS_LOCK.acquire(blocking=False):
                return self.reply(429, {"error": "diagnostics_busy"})
            try:
                payload, filename = build_report(REPORT_SERVICES)
            finally:
                DIAGNOSTICS_LOCK.release()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/status":
            governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
            return self.reply(200, {
                "power_profile": next((p for p, g in PROFILES.items() if g == governor), governor),
                "governor": governor,
                "version": current_version(),
                "build": current_build(),
                "update_channel": current_update_channel(),
                "filter": _read_filter_state(),
                "update": update_state_view(),
            })
        self.reply(404, {"error": "not_found"})

    def do_POST(self):
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        payload: dict = {}
        if self.path == "/reboot":
            action, target = "reboot", None
        elif self.path == "/action":
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                data = json.loads(self.rfile.read(length))
                action = data.get("action")
                target = data.get("target")
                payload = data if isinstance(data, dict) else {}
            except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                return self.reply(400, {"error": "invalid_json"})
        else:
            return self.reply(404, {"error": "not_found"})

        if action == "power_profile" and target in PROFILES:
            try:
                set_profile(target)
            except (OSError, ValueError) as error:
                return self.reply(503, {"error": "power_profile_unavailable"})
            return self.reply(200, {"ok": True, "power_profile": target})
        if action == "get_system_status":
            try:
                return self.reply(200, {"ok": True, **system_status()})
            except OSError:
                return self.reply(503, {"error": "system_status_unavailable"})
        if action == "wifi_scan":
            try:
                return self.reply(200, {"ok": True, "networks": wifi_scan()})
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                return self.reply(503, {"error": "wifi_scan_failed"})
        if action == "wifi_enable":
            try:
                _nmcli("radio", "wifi", "on")
                return self.reply(200, {"ok": True, **system_status()})
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                return self.reply(503, {"error": "wifi_enable_failed"})
        if action == "wifi_connect":
            try:
                return self.reply(200, {"ok": True, **wifi_connect(target)})
            except ValueError as error:
                return self.reply(400, {"error": str(error)})
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                return self.reply(503, {"error": "wifi_connection_failed"})
        # "all" is the WebUI's "Genstart Dantherm-styringen": the allowlisted
        # control services in one systemctl call. The admin helper itself is
        # not restarted, so it can still answer while the gateway comes back.
        if action == "restart_service" and (target in SERVICES or target == "all"):
            self.reply(202, {"ok": True, "message": "service restart scheduled"})
            _schedule_service_restart(list(SERVICES.values()) if target == "all" else SERVICES[target], 0.5)
            return
        if action == "get_update_channel":
            return self.reply(200, {"ok": True, "channel": current_update_channel()})
        if action == "dismiss_update_error":
            if UPDATE_STATE["running"]:
                return self.reply(409, {"error": "update_already_running", "update": update_state_view()})
            if UPDATE_STATE.get("phase") == "failed":
                _clear_update_error("Fejlen er skjult.")
            return self.reply(200, {"ok": True, "update": update_state_view()})
        if action == "get_update_status":
            return self.reply(200, {
                "ok": True,
                "channel": current_update_channel(),
                "current_version": current_version(),
                "current_build": current_build(),
                "update": update_state_view(),
            })
        if action == "set_update_channel" and target in {"stable", "beta"}:
            try:
                channel = set_update_channel(str(target))
            except OSError as error:
                return self.reply(500, {"error": f"channel_save_failed: {error}"})
            return self.reply(200, {"ok": True, "channel": channel})
        if action == "get_filter_config":
            return self.reply(200, {"ok": True, "enabled": True, **_read_filter_state()})
        if action == "set_filter_interval":
            try:
                interval = int(target)
                state = _write_filter_state(interval_days=interval)
            except (TypeError, ValueError, OSError) as error:
                return self.reply(400, {"error": f"filter_interval_failed: {error}"})
            self.reply(200, {"ok": True, "enabled": True, **state})
            _schedule_service_restart(SERVICES["gateway"])
            return
        if action == "reset_filter":
            try:
                state = _write_filter_state(reset_now=True)
            except (ValueError, OSError) as error:
                return self.reply(500, {"error": f"filter_reset_failed: {error}"})
            self.reply(200, {"ok": True, "enabled": True, **state})
            _schedule_service_restart(SERVICES["gateway"])
            return
        if action == "check_update":
            channel = str(target) if target in {"stable", "beta"} else current_update_channel()
            try:
                return self.reply(200, update_info(channel, force_refresh=True))
            except (OSError, ValueError, KeyError, urllib.error.URLError, json.JSONDecodeError) as error:
                return self.reply(503, {"error": f"update_check_failed: {error}"})
        if action == "install_update":
            channel = str(target) if target in {"stable", "beta"} else current_update_channel()
            force = payload.get("force") is True
            if not force and frost_lock_latched():
                # Updates wait while it freezes unless chosen anyway (the WebUI asks).
                return self.reply(423, {"error": FROST_DEFER_MESSAGE, "frost_deferred": True})
            if not claim_update():
                return self.reply(409, {"error": "update_already_running", "update": update_state_view()})
            try:
                set_update_channel(channel)
            except OSError as error:
                release_update_claim()
                return self.reply(500, {"error": f"channel_save_failed: {error}"})
            self.reply(202, {"ok": True, "message": "update scheduled", "channel": channel})
            threading.Thread(target=_install_update, args=(channel, force), daemon=True).start()
            return
        command = {"reboot": ["systemctl", "reboot"], "shutdown": ["systemctl", "poweroff"]}.get(action)
        if command:
            self.reply(202, {"ok": True, "message": f"{action} scheduled"})
            threading.Thread(target=lambda: (time.sleep(1), subprocess.run(command, check=False)), daemon=True).start()
            return
        self.reply(400, {"error": "action_not_allowed"})

    def log_message(self, *args):
        pass


def main() -> None:
    load_update_state()
    if PROFILE_FILE.exists():
        saved = PROFILE_FILE.read_text().strip()
        if saved in PROFILES:
            try:
                set_profile(saved)
            except (OSError, ValueError):
                pass
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
