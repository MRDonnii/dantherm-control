"""Named API keys for programs (Home Assistant, Kontrolrum cards, scripts).

Each key has its own name and scope, is shown once when it is made, and only
its SHA-256 and last four characters are stored (api-keys.json, 0600).

The single controller token from 1.8.x and earlier (controller-token.json, or
DANTHERM_CONTROLLER_TOKEN from the installer) is moved in as the key "Home
Assistant" with the scope "control" the first time this runs, so the Home
Assistant integration keeps working without any change. The move is fail-safe:

* controller-token.json is never changed by the move, so a rollback to 1.8.x
  still finds it. It is only deleted when the owner deletes the moved key.
* api-keys.json is written to a temporary file, flushed and renamed into place,
  then read back. If the moved key cannot be found in what was written, the
  file is removed again and the old token keeps working.
* Whenever api-keys.json cannot be read or written, the old token is accepted
  with the same rights as before (legacy mode). The ventilation never loses its
  Home Assistant control because of this module.

Scopes and the controller endpoints they open:

* read:    GET  /api/controller/state
* control: read + POST /api/controller/heartbeat, /inputs, /signals, /command
* admin:   control + POST /api/controller/config (settings, like a technician)

No key can manage users, keys, the Kontrolrum sign-in or the Pi itself.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("passivelink-api-keys")

PREFIX = "dth_"
SCOPES = ("read", "control", "admin")
SCOPE_RANK = {"read": 1, "control": 2, "admin": 3}
SCOPE_LABELS = {"read": "Kun læse", "control": "Læse og styre", "admin": "Administrator"}
ENDPOINT_SCOPES = {
    ("GET", "/api/controller/state"): "read",
    ("POST", "/api/controller/heartbeat"): "control",
    ("POST", "/api/controller/inputs"): "control",
    ("POST", "/api/controller/signals"): "control",
    ("POST", "/api/controller/command"): "control",
    ("POST", "/api/controller/config"): "admin",
    # Lets update.sh confirm after an update that the keys survived.
    ("GET", "/api/controller/access-health"): "read",
}
EXPIRY_DAYS = (0, 30, 90, 365)
MAX_KEYS = 50
MAX_NAME = 48
MIN_LEGACY_LENGTH = 1  # the old token worked at any length; keep that
# Usage is written at most this often, so the SD card is not worn by polling.
USAGE_WRITE_EVERY = 60.0
# Requests per minute and key. Home Assistant sends power and weather every
# 10 s and room inputs on every change, so "write" is the feed, not commands.
RATE_LIMITS = {"read": 120, "write": 240, "command": 60}
RATE_WINDOW = 60.0
# Unknown keys from one address before it is told to wait.
BAD_KEY_LIMIT = 10
BAD_KEY_WINDOW = 300.0
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "cf-ray", "true-client-ip")
UPDATE_CHECK_NAME = "Opdateringstjek"
# Loopback, link-local and the private ranges (RFC 1918, IPv6 ULA). Spelled
# out, because ipaddress.is_private also counts documentation and other ranges.
HOME_NETWORKS = tuple(ipaddress.ip_network(net) for net in (
    "127.0.0.0/8", "169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "::1/128", "fe80::/10", "fc00::/7"))


def digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def hint(key: str) -> str:
    return key[-4:]


def is_home_network(ip: str, headers=None) -> bool:
    """The caller is on the local network and did not come through a proxy or tunnel."""
    if headers is not None:
        names = {str(name).lower() for name in headers.keys()}
        if names.intersection(PROXY_HEADERS):
            return False
    try:
        address = ipaddress.ip_address(str(ip or "").strip())
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    return any(address in network for network in HOME_NETWORKS if network.version == address.version)


def is_loopback(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(str(ip or "").strip())
    except ValueError:
        return False
    address = getattr(address, "ipv4_mapped", None) or address
    return address.is_loopback


def rate_class(method: str, path: str) -> str:
    if method == "GET":
        return "read"
    return "command" if path in ("/api/controller/command", "/api/controller/config") else "write"


@dataclass
class AuthResult:
    ok: bool
    status: int = 200
    error: str = ""
    key_id: str = ""
    name: str = ""
    scope: str = ""
    moved: bool = False
    retry_after: int = 0

    @property
    def source(self) -> str:
        """What the change log shows. The moved key stays "home_assistant" like before."""
        return "home_assistant" if self.moved else f"apikey:{self.name}"


@dataclass
class _Usage:
    last_used: float | None = None
    last_ip: str = ""
    uses: int = 0
    written: float = 0.0


@dataclass
class _Limits:
    hits: dict = field(default_factory=dict)
    bad: dict = field(default_factory=dict)


class ApiKeyStore:
    def __init__(self, path, legacy, *, update_check_path=None, clock=time.time):
        """``legacy`` is the 1.8.x ControllerTokenStore (file + installer token).
        ``path`` may be a callable, so the file follows a state directory that
        is chosen later (the tests do that)."""
        self._path = path
        self.legacy = legacy
        self._update_check_path = update_check_path
        self.clock = clock
        self.lock = threading.RLock()
        self._cache: tuple[float, dict] | None = None
        self._usage: dict[str, _Usage] = {}
        self._usage_loaded = False
        self._limits = _Limits()
        self._mode = "unknown"  # ok | legacy
        self._mode_reason = ""
        self._next_attempt = 0.0
        self.rejected: dict | None = None

    # ------------------------------------------------------------ storage
    @property
    def path(self) -> Path:
        return Path(self._path() if callable(self._path) else self._path)

    @path.setter
    def path(self, value) -> None:
        self._path = value
        self._cache = None

    @property
    def update_check_path(self) -> Path:
        return Path(self._update_check_path) if self._update_check_path else self.path.with_name("update-check.json")

    @property
    def usage_path(self) -> Path:
        return self.path.with_name("api-keys-usage.json")

    def _read(self) -> dict | None:
        """The stored data, None when there is no file, ValueError when it is broken."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._cache = None
            return None
        if self._cache and self._cache[0] == mtime:
            return self._cache[1]
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
            raise ValueError("api-keys.json has no key list")
        for item in data["keys"]:
            if not (isinstance(item, dict) and isinstance(item.get("sha256"), str) and len(item["sha256"]) == 64
                    and item.get("scope") in SCOPES and isinstance(item.get("id"), str)):
                raise ValueError("api-keys.json has a broken key")
        data.setdefault("retired", [])
        self._cache = (mtime, data)
        return data

    def _write(self, data: dict) -> None:
        """Write atomically: temporary file, flush, rename, flush the directory."""
        self.path.parent.stat()  # never create the state directory here
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        try:
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        self._cache = None

    # ------------------------------------------------------------ migration
    def _legacy_token(self) -> tuple[str | None, str]:
        try:
            status = self.legacy.status()
            token = self.legacy.current()
        except Exception:  # noqa: BLE001 - a broken old file must not stop anything
            return None, "none"
        if not token or len(token) < MIN_LEGACY_LENGTH:
            return None, "none"
        return token, status.get("source") or "none"

    def _legacy_entry(self, token: str, source: str, now: float) -> dict:
        created = None
        try:
            created = self.legacy.status().get("created_at")
        except Exception:  # noqa: BLE001
            pass
        return {
            "id": secrets.token_hex(6),
            "name": "Home Assistant" if source == "webui" else "Home Assistant (fra installeren)",
            "scope": "control",
            "sha256": digest(token),
            "hint": hint(token),
            "prefix": "",
            "created": created or now,
            "created_by": "flyttet fra den gamle nøgle",
            "moved": True,
            "moved_at": now,
            "moved_from": source,
            # The old token worked from anywhere; the move must not change that.
            "lan_only": False,
            "expires": None,
        }

    def _migrate(self) -> None:
        """Move the current 1.8.x token in, unless it is already there or was deleted."""
        token, source = self._legacy_token()
        try:
            data = self._read()
        except (OSError, ValueError) as error:
            self._set_mode("legacy", f"api-keys.json kan ikke læses ({type(error).__name__})")
            return
        if token is None:
            self._set_mode("ok", "")
            return
        wanted = digest(token)
        if data is not None:
            known = {item["sha256"] for item in data["keys"]} | set(data.get("retired") or [])
            if wanted in known:
                self._set_mode("ok", "")
                return
        now = self.clock()
        fresh = data is None
        data = json.loads(json.dumps(data)) if data else {"version": 1, "keys": [], "retired": []}
        entry = self._legacy_entry(token, source, now)
        if any(item["name"] == entry["name"] for item in data["keys"]):
            entry["name"] += " (flyttet igen)"
        data["keys"].append(entry)
        # While a WebUI token exists, the installer token does not work today.
        # Keep it that way, so it does not come back if the WebUI one is deleted.
        env = getattr(self.legacy, "env_token", None)
        if source == "webui" and env and len(env) >= MIN_LEGACY_LENGTH and digest(env) != wanted:
            if digest(env) not in data["retired"]:
                data["retired"].append(digest(env))
        data.setdefault("migrations", []).append({"at": now, "from": source, "hint": hint(token)})
        try:
            self._write(data)
            check = self._read()
            if not check or not any(hmac.compare_digest(item["sha256"], wanted) for item in check["keys"]):
                raise ValueError("the moved key is missing after writing")
        except (OSError, ValueError) as error:
            if fresh:
                try:
                    self.path.unlink()
                except OSError:
                    pass
                self._cache = None
            self._set_mode("legacy", f"Den gamle nøgle kunne ikke flyttes ({type(error).__name__})")
            return
        LOG.info("Moved the old controller token (…%s, %s) to the API key \"%s\"", hint(token), source,
                 data["keys"][-1]["name"])
        self._set_mode("ok", "")

    def _set_mode(self, mode: str, reason: str) -> None:
        if mode == "legacy" and self._mode != "legacy":
            LOG.warning("API keys are in fallback mode, the old token still works: %s", reason)
        self._mode, self._mode_reason = mode, reason
        if mode == "legacy":
            self._next_attempt = self.clock() + 600

    def ensure(self) -> str:
        """Run the move if needed. Returns ok or legacy."""
        with self.lock:
            if self._mode == "unknown" or (self._mode == "legacy" and self.clock() >= self._next_attempt):
                self._migrate()
            elif self._mode == "ok":
                try:
                    self._read()
                except (OSError, ValueError) as error:
                    self._set_mode("legacy", f"api-keys.json kan ikke læses ({type(error).__name__})")
            return self._mode

    def mode(self) -> dict:
        state = self.ensure()
        return {"store": state, "store_problem": self._mode_reason if state == "legacy" else ""}

    # ------------------------------------------------------------ usage
    def _load_usage(self) -> None:
        if self._usage_loaded:
            return
        self._usage_loaded = True
        try:
            saved = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        for key_id, item in (saved.get("keys") or {}).items():
            if isinstance(item, dict):
                self._usage[str(key_id)] = _Usage(item.get("last_used"), str(item.get("last_ip") or ""), int(item.get("uses") or 0))
        if isinstance(saved.get("rejected"), dict) and self.rejected is None:
            self.rejected = saved["rejected"]

    def _save_usage(self) -> None:
        payload = {"keys": {k: {"last_used": u.last_used, "last_ip": u.last_ip, "uses": u.uses} for k, u in self._usage.items()},
                   "rejected": self.rejected}
        tmp = self.usage_path.with_name(f".{self.usage_path.name}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.usage_path)
        except OSError:
            pass  # usage is nice to have, never a reason to refuse a request

    def _note_use(self, key_id: str, ip: str, now: float) -> None:
        self._load_usage()
        usage = self._usage.setdefault(key_id, _Usage())
        usage.last_used, usage.last_ip, usage.uses = now, ip, usage.uses + 1
        if now - usage.written >= USAGE_WRITE_EVERY:
            usage.written = now
            self._save_usage()

    def _note_rejected(self, ip: str, supplied: str, reason: str, name: str = "") -> None:
        now = self.clock()
        previous = (self.rejected or {}).get("at") or 0
        self.rejected = {"at": now, "ip": ip, "hint": hint(supplied) if len(supplied) >= 8 else "", "reason": reason, "name": name}
        LOG.warning("Refused API key %s from %s: %s", f"\"{name}\"" if name else f"…{self.rejected['hint']}", ip, reason)
        if now - previous >= USAGE_WRITE_EVERY:
            self._load_usage()
            self._save_usage()

    # ------------------------------------------------------------ checks
    def _bad_key_wait(self, ip: str, now: float) -> int:
        hit = self._limits.bad.get(ip)
        if hit and now - hit[0] < BAD_KEY_WINDOW and hit[1] >= BAD_KEY_LIMIT:
            return max(1, int(BAD_KEY_WINDOW - (now - hit[0])))
        return 0

    def _count_bad(self, ip: str, now: float) -> None:
        hit = self._limits.bad.get(ip)
        if hit is None or now - hit[0] >= BAD_KEY_WINDOW:
            if len(self._limits.bad) > 512:
                self._limits.bad = {k: v for k, v in self._limits.bad.items() if now - v[0] < BAD_KEY_WINDOW}
            self._limits.bad[ip] = [now, 1]
        else:
            hit[1] += 1

    def _rate_wait(self, key_id: str, kind: str, now: float) -> int:
        slot = (key_id, kind)
        hit = self._limits.hits.get(slot)
        if hit is None or now - hit[0] >= RATE_WINDOW:
            self._limits.hits[slot] = [now, 1]
            return 0
        hit[1] += 1
        if hit[1] > RATE_LIMITS[kind]:
            return max(1, int(RATE_WINDOW - (now - hit[0])))
        return 0

    def _update_check(self, wanted: str, ip: str, now: float) -> bool:
        """The short-lived key update.sh makes for its health check (127.0.0.1 only)."""
        if not is_loopback(ip):
            return False
        try:
            data = json.loads(self.update_check_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        stored = data.get("sha256") if isinstance(data, dict) else None
        expires = data.get("expires") if isinstance(data, dict) else None
        return (isinstance(stored, str) and len(stored) == 64 and isinstance(expires, (int, float))
                and now < expires and hmac.compare_digest(stored, wanted))

    def authenticate(self, authorization: str, ip: str, method: str, path: str, headers=None) -> AuthResult:
        """Check one request. ``authorization`` is the raw Authorization header."""
        needed = ENDPOINT_SCOPES.get((method, path))
        if needed is None:
            return AuthResult(False, 403, "API-nøgler kan ikke bruges her")
        if not authorization.startswith("Bearer "):
            return AuthResult(False, 401, "API-nøgle mangler")
        supplied = authorization[7:].strip()
        if not supplied or len(supplied) > 256:
            return AuthResult(False, 401, "API-nøglen er ugyldig")
        now = self.clock()
        wanted = digest(supplied)
        with self.lock:
            state = self.ensure()
            if state == "legacy":
                token, _source = self._legacy_token()
                if token and hmac.compare_digest(digest(token), wanted):
                    return AuthResult(True, key_id="legacy", name="Home Assistant", scope="control", moved=True)
                found = None
            else:
                found = None
                data = self._read() or {"keys": []}
                for item in data["keys"]:
                    if hmac.compare_digest(item["sha256"], wanted):
                        found = item
            if found is None and needed == "read" and self._update_check(wanted, ip, now):
                return AuthResult(True, key_id="update-check", name=UPDATE_CHECK_NAME, scope="read")
            if found is None or found.get("revoked") or (found.get("expires") and now >= found["expires"]):
                wait = self._bad_key_wait(ip, now)
                self._count_bad(ip, now)
                reason = "udløbet nøgle" if found is not None else "ukendt nøgle"
                self._note_rejected(ip, supplied, reason, found.get("name", "") if found else "")
                if wait:
                    return AuthResult(False, 429, "For mange forkerte nøgler. Vent lidt.", retry_after=wait)
                return AuthResult(False, 401, "API-nøglen er ugyldig")
            result = AuthResult(True, key_id=found["id"], name=found["name"], scope=found["scope"], moved=bool(found.get("moved")))
            if found.get("lan_only", True) and not is_home_network(ip, headers):
                self._note_rejected(ip, supplied, "uden for hjemmenetværket", found["name"])
                return AuthResult(False, 403, "Nøglen må kun bruges fra hjemmenetværket", key_id=found["id"], name=found["name"])
            if SCOPE_RANK[found["scope"]] < SCOPE_RANK[needed]:
                self._note_rejected(ip, supplied, f"mangler rettigheden {SCOPE_LABELS[needed]}", found["name"])
                return AuthResult(False, 403, f"Nøglen har ikke rettigheden {SCOPE_LABELS[needed]}", key_id=found["id"], name=found["name"])
            wait = self._rate_wait(found["id"], rate_class(method, path), now)
            if wait:
                return AuthResult(False, 429, "For mange kald. Vent lidt.", key_id=found["id"], name=found["name"], retry_after=wait)
            self._note_use(found["id"], ip, now)
            return result

    # ------------------------------------------------------------ management
    def list(self) -> dict:
        state = self.ensure()
        with self.lock:
            self._load_usage()
            keys = []
            if state == "ok":
                for item in (self._read() or {"keys": []})["keys"]:
                    usage = self._usage.get(item["id"], _Usage())
                    keys.append({
                        "id": item["id"], "name": item["name"], "scope": item["scope"],
                        "prefix": item.get("prefix", PREFIX), "hint": item.get("hint", ""),
                        "created": item.get("created"), "created_by": item.get("created_by") or "",
                        "moved": bool(item.get("moved")), "moved_from": item.get("moved_from") or "",
                        "lan_only": bool(item.get("lan_only", True)), "expires": item.get("expires"),
                        "last_used": usage.last_used, "last_ip": usage.last_ip, "uses": usage.uses,
                    })
            else:
                token, source = self._legacy_token()
                if token:
                    keys.append({"id": "legacy", "name": "Home Assistant", "scope": "control", "prefix": "",
                                 "hint": hint(token), "created": None, "created_by": "", "moved": False,
                                 "moved_from": source, "lan_only": False, "expires": None,
                                 "last_used": None, "last_ip": "", "uses": 0, "legacy": True})
            return {"keys": keys, "rejected": self.rejected, **self.mode()}

    def create(self, name, scope, lan_only=True, expires_days=0, user: str | None = None) -> tuple[str, dict]:
        name = " ".join(str(name or "").split())[:MAX_NAME]
        if not name:
            raise ValueError("Giv nøglen et navn")
        if scope not in SCOPES:
            raise ValueError("Vælg en rettighed")
        try:
            days = int(expires_days or 0)
        except (TypeError, ValueError):
            raise ValueError("Ugyldig udløbstid") from None
        if days not in EXPIRY_DAYS:
            raise ValueError("Ugyldig udløbstid")
        with self.lock:
            if self.ensure() != "ok":
                raise RuntimeError("Nøglerne kan ikke gemmes lige nu. Den gamle nøgle virker stadig.")
            data = json.loads(json.dumps(self._read() or {"version": 1, "keys": [], "retired": []}))
            if len(data["keys"]) >= MAX_KEYS:
                raise ValueError(f"Der kan højst være {MAX_KEYS} nøgler")
            if any(item["name"].lower() == name.lower() for item in data["keys"]):
                raise ValueError("Der er allerede en nøgle med det navn")
            key = new_key()
            now = self.clock()
            entry = {"id": secrets.token_hex(6), "name": name, "scope": scope, "sha256": digest(key),
                     "hint": hint(key), "prefix": PREFIX, "created": now, "created_by": user or "",
                     "moved": False, "lan_only": lan_only is not False,
                     "expires": now + days * 86400 if days else None}
            data["keys"].append(entry)
            self._write(data)
        LOG.info("API key \"%s\" (%s) created by %s", name, scope, user or "?")
        return key, entry

    def delete(self, key_id: str) -> dict:
        with self.lock:
            if self.ensure() != "ok":
                raise RuntimeError("Nøglerne kan ikke gemmes lige nu. Prøv igen om lidt.")
            data = json.loads(json.dumps(self._read() or {"version": 1, "keys": [], "retired": []}))
            found = next((item for item in data["keys"] if item["id"] == key_id), None)
            if found is None:
                raise KeyError(key_id)
            data["keys"] = [item for item in data["keys"] if item["id"] != key_id]
            if found.get("moved"):
                # The old token must not come back as a new move.
                data.setdefault("retired", [])
                for value in (found["sha256"], *self._legacy_hashes()):
                    if value not in data["retired"]:
                        data["retired"].append(value)
            self._write(data)
            if found.get("moved"):
                self._remove_legacy_file()
            self._usage.pop(key_id, None)
        LOG.info("API key \"%s\" deleted", found["name"])
        return found

    def _legacy_hashes(self) -> list[str]:
        values = []
        try:
            current = self.legacy.current()
        except Exception:  # noqa: BLE001
            current = None
        for token in (current, getattr(self.legacy, "env_token", None)):
            if token and len(token) >= MIN_LEGACY_LENGTH:
                values.append(digest(token))
        return values

    def _remove_legacy_file(self) -> None:
        """Once the moved key is deleted, the plain-text copy has no use left."""
        path = getattr(self.legacy, "path", None)
        if path is None:
            return
        try:
            Path(path).unlink()
            LOG.info("Removed the old plain-text controller token file")
        except FileNotFoundError:
            pass
        except OSError as error:
            LOG.warning("Could not remove the old controller token file: %s", type(error).__name__)
        try:
            self.legacy._cache = None
        except AttributeError:
            pass

    def health(self) -> dict:
        """For update.sh: is the store fine, and does a key that can control exist?"""
        listing = self.list()
        control = [k for k in listing["keys"] if SCOPE_RANK[k["scope"]] >= SCOPE_RANK["control"]]
        return {"store": listing["store"], "keys": len(listing["keys"]), "control_keys": len(control)}
