#!/usr/bin/env python3
"""Dashboard server variant that adds the authenticated HCH controller UI/API."""
from __future__ import annotations

import gzip
import hmac
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from controller_core import ControllerError
from dashboard_server import ASSET_TYPES, DashboardHttpServer, WEBUI_CSP
from balancing_store import BalancingError, BalancingStore
from controller_token import ControllerTokenStore
from control_replay import (REPLAY_BUSY_TEXT, REPLAY_LOCK, learned_steps, replay_afterheat, replay_local_auto,
                            replay_smart_auto)
from diagnostics import WATCHDOG_RESTART_FILE, WATCHDOG_RESTART_RETENTION, read_watchdog_restarts
from history_analysis import (ALGORITHM_FIELDS, COMPARE_FIELDS, COMPARE_WINDOWS, HEALTH_CONTEXTS, HEALTH_DAYS,
                              HEALTH_FIELDS, SCALE_FIELDS, afterheat_scale, algorithm_performance, comparison_windows,
                              update_comparison, weekly_health)
from afterheat_control import ALGORITHM_HELP, ALGORITHM_LABELS, ALGORITHMS, OVERRIDE_TEXT
from update_history import UPDATE_HISTORY_FILE, find_version_file, merged_updates, record_start
from control_history import ControlHistory
from api_keys import ApiKeyStore, SCOPE_LABELS
from event_log import ALARM_KINDS, MAX_EVENTS, EventLog
from webui_auth import ROLE_LABELS
from webui_mail import AlarmMailer, MailError, MailService
from webui_permissions import admin_action_allowed, can, capabilities, config_allowed

LOG = logging.getLogger("passivelink-controller-web")

# Sign-in from Kontrolrum is an extra. If the module cannot be loaded the WebUI
# and the ventilation control carry on, and the endpoint just answers 404.
try:
    from kontrolrum_sso import MAX_BODY as KONTROLRUM_MAX_BODY, KontrolrumSso, parse_peers
except Exception:  # noqa: BLE001 - whatever is wrong with it must stay here
    KontrolrumSso = None
    KONTROLRUM_MAX_BODY = 4096
    parse_peers = None
    LOG.warning("Kontrolrum sign-in is unavailable")

CONTROLLER_ASSETS = {
    "controller.css": "text/css; charset=utf-8",
    "controller.js": "text/javascript; charset=utf-8",
    "master_status.js": "text/javascript; charset=utf-8",
    # Sniffer assets
    "sniffer.html": "text/html; charset=utf-8",
    "sniffer.js": "text/javascript; charset=utf-8",
    "sniffer.css": "text/css; charset=utf-8",
    "kontrolrum-sso.js": "text/javascript; charset=utf-8",
}


# Vite names these after their content, so a browser may keep them for good.
IMMUTABLE_ASSET = re.compile(r"^/assets/v2-[A-Za-z0-9_-]+\.(?:js|css)$")
COMPRESSIBLE_TYPES = ("application/json", "text/", "image/svg+xml")
# Left out of /api/controller/state?compact=1. The History page and the
# legacy controller page ask for the full answer.
COMPACT_OMITTED_KEYS = ("decision_log", "change_log")
# Periods for the afterheat scale (hours) and the afterheat replay.
AFTERHEAT_SCALE_HOURS = (24, 72, 168, 720)
AFTERHEAT_REPLAY_RANGES = {"1h": 3600, "6h": 21600, "12h": 43200}
# The replay's heat per level is measured over this much history before its end.
AFTERHEAT_REPLAY_SCALE_SECONDS = 3 * 86400


class ControllerDashboardHttpServer(DashboardHttpServer):
    def __init__(self, *args, controller_runtime, **kwargs):
        super().__init__(*args, **kwargs)
        self.controller_runtime = controller_runtime
        self.control_history = ControlHistory(self.history.path.with_name("controller-inputs.sqlite3"))
        self.tokens = ControllerTokenStore(
            os.getenv("DANTHERM_CONTROLLER_TOKEN_FILE", str(self.auth.path.with_name("controller-token.json"))),
            os.getenv("DANTHERM_CONTROLLER_TOKEN"),
        )
        # Named API keys. The file follows the state directory (auth.path), and
        # the old token above is moved in the first time a key is checked.
        keys_file = os.getenv("DANTHERM_API_KEYS_FILE")
        self.api_keys = ApiKeyStore(keys_file or (lambda: self.auth.path.with_name("api-keys.json")), self.tokens)
        self.mail = MailService(os.getenv(
            "DANTHERM_WEBUI_MAIL_FILE", str(self.auth.path.with_name("webui-mail.json"))))
        self.events = EventLog(os.getenv(
            "DANTHERM_WEBUI_EVENTS_FILE", str(self.auth.path.with_name("webui-events.json"))))
        self.balancing = BalancingStore(os.getenv(
            "DANTHERM_WEBUI_BALANCING_FILE", str(self.auth.path.with_name("webui-balancing.json"))))
        # Contact from Home Assistant (any request with the right API key) and
        # requests with a wrong key, e.g. after a new key was generated.
        self.started_at = time.time()
        self.ha_last_ok: float | None = None
        self.ha_last_rejected: float | None = None
        self.ha_rejected_ip: str | None = None
        self.kontrolrum_sso = self._create_kontrolrum_sso()
        self.auth.session_guard = self._kontrolrum_session_problem
        self.alarm_mailer = AlarmMailer(self.mail, lambda: {**controller_runtime.snapshot(), **self.ha_link()}, events=self.events)
        self._record_version()

    def _create_kontrolrum_sso(self):
        if KontrolrumSso is None:
            return None
        try:
            return KontrolrumSso(self.auth.path.with_name("kontrolrum-sso.json"), "dantherm")
        except Exception as error:  # noqa: BLE001 - never stop the gateway for this
            LOG.warning("Kontrolrum sign-in is unavailable: %s", type(error).__name__)
            return None

    def _kontrolrum_session_problem(self, binding, client_ip, username) -> str:
        if self.kontrolrum_sso is None:
            return "sso_disabled"
        return self.kontrolrum_sso.session_problem(binding, client_ip, username)

    def ha_link(self, now: float | None = None) -> dict:
        """Whether Home Assistant is talking to the controller API right now."""
        now = time.time() if now is None else now
        runtime = self.controller_runtime
        contacts = [t for t in (self.ha_last_ok, getattr(runtime, "smart_inputs_received_at", None)) if t]
        last = max(contacts) if contacts else None
        lease = max(180, int(getattr(runtime, "smart_inputs_valid_for", 180) or 180) + 60)
        config = getattr(getattr(runtime, "config", None), "data", {}) or {}
        rejected = self.ha_last_rejected
        if last is not None and now - last <= lease:
            state = "online"
        elif rejected and now - rejected <= 600 and (last is None or rejected > last):
            state = "bad_token"
        elif last is not None:
            state = "offline"
        elif now - self.started_at < 300:
            state = "waiting"
        else:
            state = "never"
        return {
            "ha_link_state": state,
            "ha_link_last_contact": round(last, 1) if last else None,
            "ha_link_age_seconds": round(now - last) if last else None,
            "ha_link_required": config.get("mode") == "smart_auto",
            "ha_link_rejected_at": round(rejected, 1) if rejected else None,
            "ha_link_rejected_ip": self.ha_rejected_ip,
        }

    def _compose_snapshot(self) -> dict[str, object]:
        merged = super()._compose_snapshot()
        # The air before the afterheat coil (1-Wire T2) for the history, so the
        # afterheat analysis can measure the lift per level. Memory read only.
        try:
            onewire = getattr(self.controller_runtime, "onewire", None)
            merged["afterheat_before_temperature"] = onewire.by_role("t2") if onewire is not None else None
        except Exception:  # noqa: BLE001 - an optional history value never breaks the snapshot
            merged["afterheat_before_temperature"] = None
        return merged

    def record_additional_history(self) -> None:
        # The regulator is never held up by disk I/O for optional diagnostics.
        try:
            if hasattr(self.controller_runtime, "control_observations"):
                self.control_history.record(self.controller_runtime.control_observations())
        except Exception as error:
            LOG.warning("Control replay recording skipped: %s", error)

    def weekly_health(self, now: float | None = None, context: str = "all") -> dict:
        """The read-only weekly health report (history, alarm log, watchdog restarts)."""
        now = time.time() if now is None else now
        runtime = self.controller_runtime
        config_path = getattr(getattr(runtime, "config", None), "path", None)
        restarts = read_watchdog_restarts(Path(config_path).parent / WATCHDOG_RESTART_FILE) if config_path else []
        try:
            controller = runtime.snapshot()
        except Exception:  # noqa: BLE001 - the report shows what it can
            LOG.exception("Weekly health: controller snapshot unavailable")
            controller = {}
        return weekly_health(self.history.rows(HEALTH_DAYS * 86400, HEALTH_FIELDS, markers=True, now=now),
                             events=self.events.list(kinds=ALARM_KINDS, limit=500), restarts=restarts,
                             controller=controller, now=now, context=context,
                             history=self.history.status())

    # The controller values the afterheat algorithm view shows "now" (see afterheat_algorithm).
    ALGORITHM_NOW_KEYS = (
        "afterheat_control_mode", "afterheat_control_active", "afterheat_control_reason", "afterheat_level",
        "afterheat_level_target", "afterheat_level_wanted", "afterheat_target", "afterheat_control_error",
        "afterheat_next_action", "afterheat_algorithm", "afterheat_algorithm_label", "afterheat_algorithm_active",
        "afterheat_algorithm_wanted", "afterheat_algorithm_why", "afterheat_algorithm_state",
        "afterheat_safety_override", "afterheat_algorithm_forecast", "afterheat_model", "afterheat_ha", "afterheat_pilot_learned",
        "actual_supply_air_temperature", "heating_coil_after_temperature", "afterheat_frost_lock")

    def afterheat_algorithm(self, now: float | None = None) -> dict:
        """The afterheat algorithm view: the decision now, the results per algorithm, what is learned (read-only)."""
        now = time.time() if now is None else now
        try:
            controller = self.controller_runtime.snapshot()
        except Exception:  # noqa: BLE001 - the view shows what it can
            LOG.exception("Afterheat algorithm view: controller snapshot unavailable")
            controller = {}
        rows = self.history.rows(7 * 86400, ALGORITHM_FIELDS, now=now)
        return {
            "now": {key: controller.get(key) for key in self.ALGORITHM_NOW_KEYS},
            "performance": algorithm_performance(rows, now),
            "algorithms": [{"key": key, "label": ALGORITHM_LABELS[key], "help": ALGORITHM_HELP[key]}
                           for key in ALGORITHMS],
            "overrides": {str(code): text for code, text in OVERRIDE_TEXT.items()},
            "history": self.history.status(),
        }

    def _record_version(self) -> None:
        """Note the running version when it differs from the last one (the update comparison's timeline)."""
        try:
            entry = record_start(self.auth.path.with_name(UPDATE_HISTORY_FILE), find_version_file(Path(__file__)))
            if entry:
                LOG.info("Version %s recorded as installed at %s", entry["version"], time.ctime(entry["at"]))
        except Exception as error:  # noqa: BLE001 - a diagnostic file never stops the gateway
            LOG.warning("Version history not recorded: %s", error)

    def update_comparison(self, update_id: str | None, window: str, context: str,
                          now: float | None = None) -> dict | None:
        """Regulation quality before and after one update (read-only). None: unknown update."""
        now = time.time() if now is None else now
        updates = merged_updates(self.auth.path.parent)
        # Only an update with a known previous version can be compared.
        comparable = [index for index, item in enumerate(updates) if item["previous_version"]]
        listed = [updates[index] for index in reversed(comparable)]
        if update_id is None:
            if not comparable:
                return {"updates": [], "comparison": None, "windows": list(COMPARE_WINDOWS)}
            index = comparable[-1]
        else:
            matches = [index for index in comparable if updates[index]["id"] == update_id]
            if not matches:
                return None
            index = matches[0]
        windows = comparison_windows(updates, index, COMPARE_WINDOWS[window], now)
        sides = {}
        for name in ("before", "after"):
            start, end = windows[name]["start"], windows[name]["end"]
            sides[name] = self.history.rows(max(0, end - start), COMPARE_FIELDS, now=end) if end > start else []
        with self.events.lock:
            log = list(self.events.events)
        events_known_since = (float(log[0].get("time") or 0) if len(log) >= MAX_EVENTS else -float("inf"))
        config_path = getattr(getattr(self.controller_runtime, "config", None), "path", None)
        restarts = read_watchdog_restarts(Path(config_path).parent / WATCHDOG_RESTART_FILE) if config_path else []
        comparison = update_comparison(
            updates[index], windows, sides["before"], sides["after"], context=context, window=window,
            events=[e for e in log if e.get("kind") in ALARM_KINDS], events_known_since=events_known_since,
            restarts=restarts, restarts_known_since=now - WATCHDOG_RESTART_RETENTION)
        return {"updates": listed, "comparison": comparison, "windows": list(COMPARE_WINDOWS),
                "history": self.history.status()}

    def afterheat_replay(self, settings: dict, seconds: int, patch: dict) -> dict:
        """Replay the Pi afterheat control on stored minutes; pure simulation (see control_replay)."""
        now = time.time()
        rows = self.history.rows(seconds, (*SCALE_FIELDS, "afterheat_active"), now=now)
        scale = afterheat_scale(self.history.rows(AFTERHEAT_REPLAY_SCALE_SECONDS, SCALE_FIELDS, now=now),
                                AFTERHEAT_REPLAY_SCALE_SECONDS / 3600)
        pilot = getattr(getattr(self.controller_runtime, "engine", None), "afterheat_pilot", None)
        return replay_afterheat(settings, rows, patch, scale["reference_steps"],
                                learned_steps(pilot) if pilot is not None else None)

    def reset_base_url(self) -> str:
        configured = self.mail.load().get("base_url")
        if configured:
            return configured
        # Never trust the request's Host header for a link sent by mail.
        host = self._system_snapshot().get("network_ipv4") or f"{socket.gethostname()}.local"
        return f"http://{host}:{self.port}"

    def stop(self):
        self.alarm_mailer.stop()
        super().stop()

    def start(self) -> None:
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def _session(self):
                return dashboard.auth.authenticate_cookie(self.headers.get("Cookie", ""), self.client_address[0])

            def _require_auth(self):
                session = self._session()
                if session is not None:
                    return session
                self._login_required()
                return None

            def _login_required(self):
                # The WebUI sends the browser to the login page on this answer,
                # e.g. after a restart has ended a session without "Husk mig".
                body = json.dumps({"error": "Log ind igen", "login_required": True}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

            def _read_json(self):
                try:
                    length = min(int(self.headers.get("Content-Length", "0")), 262144)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    return payload if isinstance(payload, dict) else None
                except (ValueError, TypeError, json.JSONDecodeError):
                    return None

            def _csrf(self, session):
                return session is not None and hmac.compare_digest(
                    self.headers.get("X-CSRF-Token", ""), session.get("csrf") or ""
                )

            def _need(self, session, capability):
                if can(session.get("role", "user"), capability):
                    return True
                self._json_error(403, "Din brugerrolle har ikke adgang til denne funktion")
                return False

            def _require(self, capability):
                session = self._require_auth()
                if session is None:
                    return None
                return session if self._need(session, capability) else None

            def _api_key(self, method, path):
                """Check the API key on a program's request. Never fails open."""
                try:
                    result = dashboard.api_keys.authenticate(
                        self.headers.get("Authorization", ""), self.client_address[0], method, path, self.headers)
                except Exception:  # noqa: BLE001 - a bug here must not crash the request thread
                    LOG.exception("API key check failed")
                    from api_keys import AuthResult
                    return AuthResult(False, 503, "API-nøglen kunne ikke kontrolleres")
                if result.ok and result.scope in ("control", "admin"):
                    dashboard.ha_last_ok = time.time()
                elif not result.ok and result.status in (401, 429):
                    dashboard.ha_last_rejected = time.time()
                    dashboard.ha_rejected_ip = self.client_address[0]
                return result

            def _api_key_error(self, result):
                body = json.dumps({"error": result.error}).encode()
                self.send_response(result.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                if result.retry_after:
                    self.send_header("Retry-After", str(result.retry_after))
                if result.status == 401:
                    self.send_header("WWW-Authenticate", 'Bearer realm="dantherm"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path in ("/api/controller/state", "/api/controller/access-health", "/api/controller/rooms") \
                        and self.headers.get("Authorization", "").startswith("Bearer "):
                    result = self._api_key("GET", parsed.path)
                    if result.ok:
                        if parsed.path == "/api/controller/access-health":
                            return self._json(dashboard.api_keys.health())
                        if parsed.path == "/api/controller/rooms":
                            with_catalog = parse_qs(parsed.query).get("catalog", ["1"])[0] != "0"
                            return self._json(dashboard.controller_runtime.rooms_view(include_catalog=with_catalog))
                        return self._json(self._controller_state(dashboard.controller_runtime.snapshot(), parsed))
                    # A browser with a session still gets the page's own answer below.
                    if parsed.path == "/api/controller/access-health" or (
                            self._session() is None and dashboard.auth.configured() and dashboard.auth.enabled()):
                        return self._api_key_error(result)
                if parsed.path == "/api/onewire/water" and self.client_address[0] in ("127.0.0.1", "::1"):
                    # Read by the local 1-Wire service so its flow/return follow the WebUI.
                    self._json(dashboard.controller_runtime.onewire.water_assignment())
                    return
                if parsed.path == "/api/auth/kontrolrum/status":
                    if self._require("users") is None:
                        return
                    if dashboard.kontrolrum_sso is None:
                        return self._json_error(503, "Login fra Kontrolrum er ikke tilgængeligt")
                    return self._json(dashboard.kontrolrum_sso.status())
                if parsed.path == "/api/auth/status":
                    session = self._session()
                    role = session.get("role", "user") if session else None
                    user = dashboard.auth.user(session["username"]) if session else None
                    mail = dashboard.mail.load()
                    self._json({
                        "configured": dashboard.auth.configured(),
                        "enabled": dashboard.auth.enabled(),
                        "authenticated": session is not None,
                        "username": session.get("username") if session else None,
                        "csrf": session.get("csrf") if session else None,
                        "remembered": bool(session and session.get("remember")),
                        "role": role,
                        "role_label": ROLE_LABELS.get(role) if role else None,
                        "permissions": capabilities(role) if role else [],
                        "email": (user or {}).get("email", "") if session else None,
                        "password_reset_available": bool(dashboard.mail.configured(mail) and mail.get("password_reset_enabled")),
                    })
                    return
                if parsed.path == "/api/auth/reset/check":
                    token = parse_qs(parsed.query).get("token", [""])[0]
                    self._json({"valid": dashboard.auth.reset_token_valid(token)})
                    return
                if parsed.path in ("/login", "/setup", "/forgot", "/reset"):
                    self._file(dashboard.web_root / "login.html", "text/html; charset=utf-8")
                    return
                if parsed.path in ("/assets/auth.css", "/assets/auth.js", "/assets/favicon.svg", "/assets/apple-touch-icon.png", "/assets/brand-mark.svg"):
                    target = dashboard.web_root / Path(parsed.path).name
                    self._file(target, ASSET_TYPES.get(target.suffix))
                    return
                if not dashboard.auth.configured():
                    self.send_response(302); self.send_header("Location", "/setup"); self.end_headers(); return
                if self._session() is None and dashboard.auth.enabled():
                    if parsed.path in ("/state.json", "/api", "/history.json") or parsed.path.startswith("/api/"):
                        # A redirect would hand fetch() the login page instead of JSON.
                        self._login_required(); return
                    self.send_response(302); self.send_header("Location", "/login"); self.end_headers(); return

                if parsed.path in ("/", "/index.html"):
                    self._file(dashboard.web_root / "index.html", "text/html; charset=utf-8")
                elif parsed.path in ("/controller", "/controller.html"):
                    if self._require("configure") is not None:
                        self._file(dashboard.web_root / "controller.html", "text/html; charset=utf-8")
                elif parsed.path == "/kontrolrum-sso":
                    if self._require("users") is not None:
                        self._file(dashboard.web_root / "kontrolrum-sso.html", "text/html; charset=utf-8")
                elif parsed.path == "/sniffer":
                    # Sniffer UI (technicians and administrators)
                    if self._require("diagnostics") is not None:
                        self._file(dashboard.web_root / "sniffer.html", "text/html; charset=utf-8")
                elif parsed.path == "/api/sniffer/status":
                    if self._require("diagnostics") is None:
                        return
                    self._json(getattr(dashboard, "sniffer", {}).status() if getattr(dashboard, "sniffer", None) else {"running": False})
                elif parsed.path == "/api/sniffer/recent":
                    if self._require("diagnostics") is None:
                        return
                    recent = getattr(dashboard, "sniffer", None)
                    self._json(recent.recent_frames() if recent else [])
                elif parsed.path == "/api/users":
                    if self._require("users") is None:
                        return
                    self._json({"users": dashboard.auth.list_users(), "roles": ROLE_LABELS})
                elif parsed.path == "/api/events":
                    session = self._require_auth()
                    if session is None:
                        return
                    # Sign-ins and user changes are for administrators only.
                    kinds = None if can(session.get("role", "user"), "users") else ALARM_KINDS
                    try:
                        limit = max(1, min(500, int(parse_qs(parsed.query).get("limit", ["200"])[0])))
                    except ValueError:
                        limit = 200
                    self._json({"events": dashboard.events.list(kinds=kinds, limit=limit),
                                "active": dashboard.events.active_alarms()})
                elif parsed.path == "/api/balancing":
                    if self._require("configure") is not None:
                        self._json(dashboard.balancing.state())
                elif parsed.path == "/api/balancing/report":
                    if self._require("configure") is None:
                        return
                    report = dashboard.balancing.get_report(parse_qs(parsed.query).get("id", [""])[0])
                    self._json(report) if report else self._json_error(404, "Rapporten findes ikke")
                elif parsed.path == "/api/integration":
                    if self._require("configure") is None:
                        return
                    health = dashboard.api_keys.health()
                    self._json({"configured": health["control_keys"] > 0, "control_keys": health["control_keys"],
                                "keys": health["keys"], "store": health["store"], "port": dashboard.port,
                                "address": dashboard._system_snapshot().get("network_ipv4")})
                elif parsed.path == "/api/access/keys":
                    if self._require("users") is None:
                        return
                    self._json(dashboard.api_keys.list())
                elif parsed.path == "/api/mail":
                    if self._require("mail") is None:
                        return
                    self._json(dashboard.mail.public())
                elif parsed.path == "/api/controller/rooms":
                    # The room setup and HA's sensor catalogue for the page "Rum".
                    if self._require("configure") is not None:
                        self._json(dashboard.controller_runtime.rooms_view())
                elif parsed.path == "/api/controller/state":
                    self._json(self._controller_state({**dashboard.controller_runtime.snapshot(), **dashboard.ha_link(),
                        "smart_replay_history": dashboard.control_history.status(),
                        "history_recording": dashboard.history.status()}, parsed))
                elif parsed.path in ("/state.json", "/api"):
                    self._json({**dashboard.snapshot(), **dashboard.ha_link(),
                                "weather": dashboard.controller_runtime.weather_snapshot()})
                elif parsed.path == "/api/afterheat/scale":
                    # Read-only: what one HAC1 level is worth, measured in the history.
                    try:
                        hours = int(parse_qs(parsed.query).get("hours", ["168"])[0])
                    except ValueError:
                        hours = 0
                    if hours not in AFTERHEAT_SCALE_HOURS:
                        return self._json_error(400, "Ugyldig periode")
                    self._json(afterheat_scale(dashboard.history.rows(hours * 3600, SCALE_FIELDS), hours))
                elif parsed.path == "/api/health/weekly":
                    if self._require("diagnostics") is not None:
                        context = parse_qs(parsed.query).get("context", ["all"])[0]
                        if context not in HEALTH_CONTEXTS:
                            return self._json_error(400, "Ugyldig driftstilstand")
                        self._json(dashboard.weekly_health(context=context))
                elif parsed.path == "/api/health/update-comparison":
                    if self._require("diagnostics") is not None:
                        query = parse_qs(parsed.query)
                        context = query.get("context", ["all"])[0]
                        window = query.get("window", ["24h"])[0]
                        update_id = query.get("update", [None])[0]
                        if context not in HEALTH_CONTEXTS:
                            return self._json_error(400, "Ugyldig driftstilstand")
                        if window not in COMPARE_WINDOWS:
                            return self._json_error(400, "Ugyldig periode")
                        if update_id is not None and not re.fullmatch(r"\d{1,12}", update_id):
                            return self._json_error(400, "Ugyldig opdatering")
                        result = dashboard.update_comparison(update_id, window, context)
                        if result is None:
                            return self._json_error(400, "Ukendt opdatering")
                        self._json(result)
                elif parsed.path == "/api/afterheat/algorithm":
                    if self._require("diagnostics") is not None:
                        self._json(dashboard.afterheat_algorithm())
                elif parsed.path == "/api/diagnostics/report":
                    if self._require("diagnostics") is not None:
                        self._diagnostic_report()
                elif parsed.path == "/history.json":
                    name = parse_qs(parsed.query).get("range", ["24h"])[0]
                    self._json({"range": name, "samples": dashboard.history.query(name),
                                "recording": dashboard.history.status()})
                elif parsed.path.startswith("/assets/"):
                    name = Path(parsed.path).name
                    target = dashboard.web_root / name
                    content_type = CONTROLLER_ASSETS.get(name) or ASSET_TYPES.get(target.suffix)
                    self._file(target, content_type) if content_type else self.send_error(404)
                else:
                    self.send_error(404)

            @staticmethod
            def _controller_state(state, parsed):
                if parse_qs(parsed.query).get("compact") == ["1"]:
                    # Pollers every few seconds do not show the logs, which
                    # are about 90 % of the answer.
                    for key in COMPACT_OMITTED_KEYS:
                        state.pop(key, None)
                return state

            def _diagnostic_report(self):
                if not dashboard.admin_token:
                    return self.send_error(503, "Diagnostic helper unavailable")
                request = urllib.request.Request(
                    f"{dashboard.admin_url.rstrip('/')}/diagnostics",
                    headers={"Authorization": f"Bearer {dashboard.admin_token}"},
                )
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=60) as response:
                        helper_payload = response.read(8 * 1024 * 1024 + 1)
                        disposition = response.headers.get("Content-Disposition", "")
                except (OSError, urllib.error.URLError):
                    return self.send_error(503, "Diagnostic helper unavailable")
                if len(helper_payload) > 8 * 1024 * 1024:
                    return self.send_error(413, "Diagnostic report too large")
                snapshot = dashboard.snapshot()
                snapshot["controller"] = dashboard.controller_runtime.snapshot()
                safe_snapshot = {
                    key: ("[REDACTED]" if any(word in key.lower() for word in
                          ("token", "password", "secret", "cookie", "authorization")) else value)
                    for key, value in snapshot.items()
                }
                state = json.dumps(safe_snapshot, indent=2, ensure_ascii=False, default=str).encode("utf-8")
                body = helper_payload + b"\n\n==================== PASSIVELINK CURRENT STATE ====================\n" + state + b"\n"
                filename = "dantherm-debug.txt"
                if 'filename="' in disposition:
                    filename = disposition.split('filename="', 1)[1].split('"', 1)[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

            def do_POST(self):
                if self.path == "/api/auth/setup":
                    if dashboard.auth.configured():
                        return self._json_error(409, "Allerede konfigureret")
                    data = self._read_json() or {}
                    try:
                        dashboard.auth.save(data.get("username", ""), data.get("password", ""), True)
                    except ValueError as error:
                        return self._json_error(400, str(error))
                    except OSError:
                        LOG.exception("Could not save the initial WebUI user")
                        return self._json_error(500, "Kunne ikke gemme login-konfigurationen")
                    try:
                        remember = data.get("remember") is True
                        sid, csrf = dashboard.auth.session(data["username"].strip(), remember=remember)
                    except OSError:
                        LOG.exception("Could not create the initial WebUI session")
                        return self._json_error(500, "Kunne ikke gemme login-sessionen")
                    return self._login_reply(sid, csrf)

                if self.path == "/api/auth/login":
                    data = self._read_json() or {}
                    ip = self.client_address[0]
                    if not dashboard.auth.allow_attempt(ip):
                        return self._json_error(429, "For mange forsøg. Vent fem minutter.")
                    try:
                        verified = dashboard.auth.verify(data.get("username", ""), data.get("password", ""))
                    except (OSError, ValueError):
                        LOG.exception("Could not read the WebUI authentication store")
                        return self._json_error(500, "Kunne ikke læse login-konfigurationen")
                    if not verified:
                        dashboard.auth.failed(ip)
                        dashboard.events.add("login_failed", "Mislykket login", user=str(data.get("username", ""))[:64], ip=ip)
                        return self._json_error(401, "Forkert brugernavn eller adgangskode")
                    try:
                        remember = data.get("remember") is True
                        sid, csrf = dashboard.auth.session(data["username"], remember=remember)
                    except OSError:
                        LOG.exception("Could not create the WebUI login session")
                        return self._json_error(500, "Kunne ikke gemme login-sessionen")
                    dashboard.auth.record_login(data["username"])
                    dashboard.events.add("login", "Logget ind", user=str(data["username"])[:64], ip=ip)
                    return self._login_reply(sid, csrf)

                if self.path == "/api/auth/kontrolrum":
                    return self._kontrolrum_login()

                if self.path == "/api/auth/forgot":
                    data = self._read_json() or {}
                    key = f"forgot:{self.client_address[0]}"
                    if not dashboard.auth.allow_attempt(key):
                        return self._json_error(429, "For mange forsøg. Vent fem minutter.")
                    dashboard.auth.failed(key)
                    mail = dashboard.mail.load()
                    if not (dashboard.mail.configured(mail) and mail.get("password_reset_enabled")):
                        return self._json_error(503, "Nulstilling via mail er ikke sat op. Kontakt administratoren.")
                    found = dashboard.auth.create_reset_token(str(data.get("identifier", ""))[:254])
                    if found:
                        user, token = found
                        dashboard.mail.send_password_reset(
                            user["email"], user["username"], f"{dashboard.reset_base_url()}/reset?token={token}")
                    # Same answer whether or not the user exists.
                    return self._json({"ok": True})

                if self.path == "/api/auth/reset":
                    data = self._read_json() or {}
                    try:
                        who = dashboard.auth.reset_password(str(data.get("token", "")), str(data.get("password", "")))
                        dashboard.events.add("password_reset", "Adgangskode nulstillet via mail-link", user=who)
                    except PermissionError as error:
                        return self._json_error(400, str(error))
                    except (ValueError, KeyError) as error:
                        return self._json_error(400, str(error).strip("'"))
                    return self._json({"ok": True})

                # Machine-to-machine endpoints for Home Assistant. HA sends
                # intent and room measurements only; Pi remains source of truth.
                # The settings endpoint also takes an administrator API key.
                api_config = self.path == "/api/controller/config" and self.headers.get("Authorization", "").startswith("Bearer ")
                if api_config or self.path in ("/api/controller/heartbeat", "/api/controller/command", "/api/controller/inputs", "/api/controller/signals", "/api/controller/ha-catalog"):
                    result = self._api_key("POST", self.path)
                    if not result.ok:
                        return self._api_key_error(result)
                    data = self._read_json() or {}
                    try:
                        if self.path == "/api/controller/heartbeat":
                            return self._json(dashboard.controller_runtime.heartbeat(str(data.get("demand", "normal"))))
                        if self.path in ("/api/controller/command", "/api/controller/config"):
                            return self._json(dashboard.controller_runtime.configure(data, source=result.source))
                        if self.path == "/api/controller/signals":
                            return self._json(dashboard.controller_runtime.external_signals(data))
                        if self.path == "/api/controller/ha-catalog":
                            return self._json(dashboard.controller_runtime.ha_catalog_input(data))
                        return self._json(dashboard.controller_runtime.room_inputs(data))
                    except ControllerError as error:
                        return self._json_error(400, str(error))
                    except RuntimeError as error:
                        return self._json_error(503, str(error))

                session = self._require_auth()
                if session is None:
                    return
                if not self._csrf(session):
                    return self._json_error(403, "Ugyldig sikkerhedstoken")
                if self.path == "/api/auth/logout":
                    dashboard.auth.logout(self.headers.get("Cookie", "")); return self._json({"ok": True})
                if self.path.startswith("/api/auth/kontrolrum/"):
                    if not self._need(session, "users"):
                        return
                    return self._kontrolrum_admin(session, self._read_json() or {})
                if self.path == "/api/auth/settings":
                    data = self._read_json() or {}
                    enabled = data.get("enabled") if can(session.get("role"), "login_switch") else None
                    try:
                        dashboard.auth.update(
                            data.get("current_password", ""), data.get("username", ""),
                            data.get("password", ""), enabled, acting=session.get("username"),
                            email=data.get("email"),
                        )
                    except PermissionError as error:
                        return self._json_error(401, str(error))
                    except ValueError as error:
                        return self._json_error(400, str(error))
                    return self._json({"ok": True})
                if self.path.startswith("/api/users/"):
                    if not self._need(session, "users"):
                        return
                    return self._users_action(session, self._read_json() or {})
                if self.path.startswith("/api/balancing/"):
                    if not self._need(session, "configure"):
                        return
                    data = self._read_json() or {}
                    who = session.get("username")
                    try:
                        if self.path == "/api/balancing/project":
                            dashboard.balancing.save_project(data, who)
                            return self._json(dashboard.balancing.state())
                        if self.path == "/api/balancing/report":
                            summary = dashboard.balancing.add_report(data, who)
                            return self._json({"ok": True, "report": summary, **dashboard.balancing.state()})
                        if self.path == "/api/balancing/report/delete":
                            if not dashboard.balancing.delete_report(str(data.get("id", ""))):
                                return self._json_error(404, "Rapporten findes ikke")
                            return self._json(dashboard.balancing.state())
                    except BalancingError as error:
                        return self._json_error(400, str(error))
                    except OSError:
                        LOG.exception("Could not save balancing data")
                        return self._json_error(500, "Kunne ikke gemme indreguleringen")
                    return self.send_error(404)
                if self.path in ("/api/integration/token/generate", "/api/integration/token/reveal"):
                    # Keys are stored as a hash now and can never be shown again.
                    return self._json_error(410, "Brug Opsætning → Adgang for programmer")
                if self.path.startswith("/api/access/keys/"):
                    if not self._need(session, "users"):
                        return
                    return self._access_keys_action(session, self._read_json() or {})
                if self.path in ("/api/mail/settings", "/api/mail/test"):
                    if not self._need(session, "mail"):
                        return
                    data = self._read_json() or {}
                    try:
                        if self.path == "/api/mail/settings":
                            return self._json(dashboard.mail.update(data))
                        recipients = data.get("recipients")
                        dashboard.mail.send_test([str(r) for r in recipients] if isinstance(recipients, list) else None)
                        return self._json({"ok": True, "mail": dashboard.mail.public()})
                    except ValueError as error:
                        return self._json_error(400, str(error))
                    except MailError as error:
                        return self._json_error(502, str(error))
                    except OSError:
                        LOG.exception("Could not save mail settings")
                        return self._json_error(500, "Kunne ikke gemme mailindstillingerne")
                # Sniffer control endpoints (authenticated + CSRF, technicians)
                if self.path.startswith("/api/sniffer/") and not self._need(session, "diagnostics"):
                    return
                if self.path == "/api/sniffer/start":
                    # start a read-only capture
                    sniffer = getattr(dashboard, "sniffer", None)
                    if sniffer is None:
                        return self._json_error(503, "Sniffer unavailable")
                    return self._json(sniffer.start())
                if self.path == "/api/sniffer/stop":
                    sniffer = getattr(dashboard, "sniffer", None)
                    if sniffer is None:
                        return self._json_error(503, "Sniffer unavailable")
                    return self._json(sniffer.stop())
                if self.path == "/api/sniffer/marker":
                    sniffer = getattr(dashboard, "sniffer", None)
                    if sniffer is None:
                        return self._json_error(503, "Sniffer unavailable")
                    data = self._read_json() or {}
                    name = data.get("name") or data.get("marker") or "manual"
                    try:
                        return self._json(sniffer.marker(str(name)))
                    except Exception as e:
                        return self._json_error(500, str(e))
                if self.path == "/api/controller/replay":
                    if not self._need(session, "configure"):
                        return
                    data = self._read_json()
                    if not isinstance(data, dict):
                        return self._json_error(400, "Ugyldig prøve")
                    name = data.get("range", "24h")
                    mode = data.get("mode", "local_auto")
                    if mode == "afterheat":
                        if name not in AFTERHEAT_REPLAY_RANGES:
                            return self._json_error(400, "Vælg højst 12 timer")
                    elif name not in ("1h", "6h", "24h"):
                        return self._json_error(400, "Vælg højst 24 timer")
                    if not REPLAY_LOCK.acquire(blocking=False):
                        return self._json_error(429, REPLAY_BUSY_TEXT)
                    try:
                        with dashboard.controller_runtime.config.lock:
                            import copy
                            settings = copy.deepcopy(dashboard.controller_runtime.config.data)
                        if mode == "afterheat":
                            return self._json(dashboard.afterheat_replay(settings, AFTERHEAT_REPLAY_RANGES[name],
                                                                         data.get("settings", {})))
                        if mode == "smart_auto":
                            samples = dashboard.control_history.query({"1h": 3600, "6h": 21600, "24h": 86400}[name])
                            return self._json(replay_smart_auto(settings, samples, data.get("settings", {})))
                        if mode != "local_auto":
                            return self._json_error(400, "Ugyldig prøvetilstand")
                        return self._json(replay_local_auto(settings, dashboard.history.query(name), data.get("settings", {})))
                    except (ControllerError, TypeError, ValueError) as error:
                        return self._json_error(400, str(error))
                    finally:
                        REPLAY_LOCK.release()
                if self.path == "/api/controller/config":
                    data = self._read_json() or {}
                    denied = config_allowed(session.get("role", "user"), data)
                    if denied:
                        return self._json_error(403, "Din brugerrolle må ikke ændre: " + ", ".join(denied))
                    try:
                        return self._json(dashboard.controller_runtime.configure(data, source=f"webui:{session.get('username') or 'ukendt'}"))
                    except ControllerError as error:
                        return self._json_error(400, str(error))
                    except RuntimeError as error:
                        return self._json_error(503, str(error))
                if self.path != "/api/admin/action":
                    return self.send_error(405)
                admin_action = self._read_json() or {}
                if not admin_action_allowed(session.get("role", "user"), str(admin_action.get("action", ""))):
                    return self._json_error(403, "Kun teknikere og administratorer kan udføre denne handling")
                if not dashboard.admin_token:
                    return self.send_error(405)
                body = json.dumps(admin_action).encode()
                timeout = 45 if admin_action.get("action") in {"wifi_scan", "wifi_connect"} else 4
                request = urllib.request.Request(
                    f"{dashboard.admin_url.rstrip('/')}/action", data=body, method="POST",
                    headers={"Authorization": f"Bearer {dashboard.admin_token}", "Content-Type": "application/json"},
                )
                try:
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
                        payload, status = response.read(), response.status
                except urllib.error.HTTPError as error:
                    payload, status = error.read(), error.code
                except OSError:
                    return self.send_error(503, "Admin helper unavailable")
                restart_kind = {"reboot": "Genstart af Pi'en", "restart_service": "Genstart af Dantherm-styringen"}.get(str(admin_action.get("action", "")))
                if restart_kind and status < 300 and getattr(dashboard, "events", None) is not None:
                    try:
                        dashboard.events.add("system_restart", f"{restart_kind} startet fra WebUI", by=session.get("username"), target=admin_action.get("target"))
                    except Exception:
                        LOG.debug("Kunne ikke logge genstart", exc_info=True)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)

            def _users_action(self, session, data):
                auth = dashboard.auth
                name = str(data.get("username", ""))
                try:
                    if self.path == "/api/users/create":
                        user = auth.create_user(name, str(data.get("password", "")), str(data.get("role", "user")),
                                                data.get("email", ""), data.get("expires_at"))
                        dashboard.events.add("user_created", f"Bruger oprettet som {user['role_label']}", user=user["username"], by=session.get("username"))
                        return self._json({"ok": True, "user": user, "users": auth.list_users()})
                    if self.path == "/api/users/update":
                        changes = {key: data[key] for key in ("role", "email", "disabled", "expires_at") if key in data}
                        if data.get("password"):
                            changes["password"] = str(data["password"])
                        if name.lower() == str(session.get("username", "")).lower() and (
                                changes.get("role", "admin") != "admin" or changes.get("disabled")):
                            return self._json_error(400, "Du kan ikke fjerne din egen administratoradgang")
                        user = auth.update_user(name, **changes)
                        what = ", ".join(sorted("adgangskode" if key == "password" else key for key in changes)) or "intet"
                        dashboard.events.add("user_changed", f"Bruger ændret: {what}", user=user["username"], by=session.get("username"))
                        return self._json({"ok": True, "user": user, "users": auth.list_users()})
                    if self.path == "/api/users/delete":
                        if name.lower() == str(session.get("username", "")).lower():
                            return self._json_error(400, "Du kan ikke slette dig selv")
                        auth.delete_user(name)
                        dashboard.events.add("user_deleted", "Bruger slettet", user=name, by=session.get("username"))
                        return self._json({"ok": True, "users": auth.list_users()})
                    if self.path == "/api/users/send-reset":
                        if not dashboard.mail.ready():
                            return self._json_error(503, "Mailservicen er ikke sat op")
                        found = auth.create_reset_token(name)
                        if not found:
                            return self._json_error(400, "Brugeren har ingen e-mailadresse eller er ikke aktiv")
                        user, token = found
                        dashboard.mail.send_password_reset(
                            user["email"], user["username"], f"{dashboard.reset_base_url()}/reset?token={token}")
                        return self._json({"ok": True})
                except KeyError as error:
                    return self._json_error(404, str(error).strip("'"))
                except ValueError as error:
                    return self._json_error(400, str(error))
                except OSError:
                    LOG.exception("Could not save the WebUI user store")
                    return self._json_error(500, "Kunne ikke gemme brugerne")
                return self.send_error(404)

            def _kontrolrum_login(self):
                """POST /api/auth/kontrolrum: sign-in from Kontrolrum. Whatever
                goes wrong in here ends with this one request being refused."""
                sso = dashboard.kontrolrum_sso
                if sso is None:
                    return self._json_error(404, "not_found")
                ip = self.client_address[0]
                try:
                    refusal = sso.gate(ip)
                    if refusal is not None:
                        return self._json_error(refusal.status, refusal.error)
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        length = -1
                    raw = self.rfile.read(length) if 0 < length <= KONTROLRUM_MAX_BODY else b""
                    outcome = sso.login(ip, raw, dashboard.auth.active_username)
                    if not outcome.ok:
                        dashboard.events.add("login_failed", f"Afvist login fra Kontrolrum ({outcome.error})", ip=ip)
                        return self._json_error(outcome.status, outcome.error)
                    sid, csrf = dashboard.auth.session_kontrolrum(outcome.user, outcome.binding)
                    dashboard.auth.record_login(outcome.user)
                    dashboard.events.add("login", "Logget ind fra Kontrolrum", user=outcome.user[:64], ip=ip)
                except Exception as error:  # noqa: BLE001 - see the docstring
                    LOG.warning("Kontrolrum sign-in failed: %s", type(error).__name__)
                    return self._json_error(503, "unavailable")
                return self._login_reply(sid, csrf)

            def _access_keys_action(self, session, data):
                who = session.get("username")
                action = self.path.rsplit("/", 1)[-1]
                try:
                    if action == "create":
                        key, entry = dashboard.api_keys.create(
                            data.get("name"), data.get("scope"), data.get("lan_only") is not False,
                            data.get("expires_days") or 0, who)
                        dashboard.events.add("token_generated",
                                             f"Ny API-nøgle \"{entry['name']}\" ({SCOPE_LABELS[entry['scope']]})", user=who)
                        # The only time the key itself leaves the Pi.
                        return self._json({"key": key, "id": entry["id"], **dashboard.api_keys.list()})
                    if action == "delete":
                        found = dashboard.api_keys.delete(str(data.get("id", "")))
                        dashboard.events.add("token_deleted", f"API-nøglen \"{found['name']}\" er slettet", user=who)
                        return self._json(dashboard.api_keys.list())
                except KeyError:
                    return self._json_error(404, "Nøglen findes ikke")
                except ValueError as error:
                    return self._json_error(400, str(error))
                except RuntimeError as error:
                    return self._json_error(503, str(error))
                except OSError:
                    LOG.warning("Could not save the API keys")
                    return self._json_error(500, "Kunne ikke gemme nøglerne. Den gamle nøgle virker stadig.")
                return self.send_error(404)

            def _kontrolrum_admin(self, session, data):
                sso = dashboard.kontrolrum_sso
                if sso is None:
                    return self._json_error(503, "Login fra Kontrolrum er ikke tilgængeligt")
                who = session.get("username")
                action = self.path.rsplit("/", 1)[-1]
                try:
                    if action == "key":
                        key = sso.create_key(who)
                        dashboard.events.add("token_generated", "Ny SSO-nøgle til Kontrolrum", user=who)
                        return self._json({"key": key, "status": sso.status()})
                    if action == "off":
                        if sso.disable():
                            dashboard.events.add("user_changed", "Login fra Kontrolrum slået fra", user=who)
                    elif action == "peers":
                        good, bad = parse_peers(str(data.get("ips", ""))[:400])
                        if bad:
                            return self._json_error(400, "Kunne ikke læse: " + ", ".join(bad[:3]))
                        sso.set_peers(good)
                        dashboard.events.add("user_changed", "Kontrolrums adresse ændret: " + (", ".join(good) or "ingen"), user=who)
                    elif action == "user":
                        name = str(data.get("username", ""))[:64]
                        if not dashboard.auth.user(name):
                            return self._json_error(404, "Brugeren findes ikke")
                        allowed = data.get("allowed") is True
                        if sso.set_user(name, allowed):
                            dashboard.events.add("user_changed", f"{name} {'må' if allowed else 'må ikke'} logges ind fra Kontrolrum", user=who)
                    else:
                        return self.send_error(404)
                except ValueError as error:
                    return self._json_error(400, str(error))
                except OSError:
                    LOG.warning("Could not save the Kontrolrum sign-in settings")
                    return self._json_error(500, "Kunne ikke gemme indstillingen")
                return self._json({"status": sso.status()})

            def _login_reply(self, sid, csrf):
                max_age = dashboard.auth.session_max_age(sid)
                body = json.dumps({"ok": True, "csrf": csrf, "max_age": max_age}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"dantherm_session={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

            def _json_error(self, status, message):
                body = json.dumps({"error": message}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

            def _json(self, payload):
                self._body(json.dumps(payload, separators=(",", ":"), default=str).encode(), "application/json")

            def _file(self, path, content_type):
                try:
                    body = path.read_bytes()
                except OSError:
                    self.send_error(404); return
                self._body(body, content_type)

            def _body(self, body, content_type):
                immutable = IMMUTABLE_ASSET.match(urlparse(self.path).path) is not None
                compress = (
                    len(body) >= 1024 and str(content_type or "").startswith(COMPRESSIBLE_TYPES)
                    and "gzip" in self.headers.get("Accept-Encoding", "")
                )
                if compress:
                    body = gzip.compress(body, compresslevel=5)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "public, max-age=31536000, immutable" if immutable else "no-store")
                if compress:
                    self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", WEBUI_CSP)
                self.send_header("Content-Length", str(len(body)))
                try:
                    self.end_headers(); self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # The browser gave up (poll timeout, page closed); nothing to answer.
                    LOG.debug("Client %s closed the connection before %s was sent", self.client_address[0], self.path)

            def log_message(self, format_, *args):
                LOG.debug(format_, *args)

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, name="dashboard-http", daemon=True)
        self.thread.start()

        self.start_background()
        self.alarm_mailer.site_name = self.device_name or "HCH5 Control"
        self.alarm_mailer.start()
        LOG.info("Controller WebUI listening on %s:%s", self.host, self.port)
