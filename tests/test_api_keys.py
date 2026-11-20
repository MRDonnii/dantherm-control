import hashlib
import http.cookiejar
import json
import logging
import os
import re
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

import api_keys  # noqa: E402
from api_keys import ApiKeyStore, ENDPOINT_SCOPES, is_home_network  # noqa: E402
from controller_dashboard_server import ControllerDashboardHttpServer  # noqa: E402
from controller_token import ControllerTokenStore  # noqa: E402
from diagnostics_report import redact  # noqa: E402
from event_log import EventLog  # noqa: E402

OLD = "old-webui-token-AbCdEfGhIjKlMnOp-K7pQ"
ENV = "installer-token-0123456789abcdef"
LAN = "10.0.0.20"
PASSWORD = "long-test-password"


def bearer(key):
    return f"Bearer {key}"


def old_file(tmp, token=OLD):
    path = Path(tmp) / "controller-token.json"
    path.write_text(json.dumps({"token": token, "created_at": 1700000000.0, "created_by": "admin"}))
    os.chmod(path, 0o600)
    return path


def store(tmp, env=None, **kwargs):
    legacy = ControllerTokenStore(Path(tmp) / "controller-token.json", env)
    return ApiKeyStore(Path(tmp) / "api-keys.json", legacy, **kwargs)


class Clock:
    def __init__(self): self.now = 1_800_000_000.0
    def __call__(self): return self.now


class MigrationTests(unittest.TestCase):
    def test_webui_token_is_moved_as_home_assistant_and_the_old_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = old_file(tmp)
            before = path.read_bytes()
            keys = store(tmp, ENV)
            result = keys.authenticate(bearer(OLD), LAN, "POST", "/api/controller/command")
            self.assertTrue(result.ok)
            self.assertEqual((result.name, result.scope, result.source), ("Home Assistant", "control", "home_assistant"))
            self.assertEqual(path.read_bytes(), before)  # rollback to 1.8.x still finds it
            stored = (Path(tmp) / "api-keys.json")
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            text = stored.read_text()
            self.assertNotIn(OLD, text)
            self.assertIn(hashlib.sha256(OLD.encode()).hexdigest(), text)
            listed = keys.list()["keys"]
            self.assertEqual(len(listed), 1)
            self.assertTrue(listed[0]["moved"])
            self.assertFalse(listed[0]["lan_only"])  # worked from anywhere before, still does
            self.assertEqual(listed[0]["hint"], "K7pQ")
            self.assertNotIn(OLD, json.dumps(keys.list()))
            # The installer token did not work while a WebUI token existed; it still does not.
            self.assertFalse(keys.authenticate(bearer(ENV), LAN, "GET", "/api/controller/state").ok)

    def test_installer_token_is_moved_when_there_is_no_webui_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = store(tmp, ENV)
            result = keys.authenticate(bearer(ENV), LAN, "POST", "/api/controller/signals")
            self.assertTrue(result.ok)
            self.assertEqual(result.name, "Home Assistant (fra installeren)")

    def test_short_installer_token_keeps_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(store(tmp, "short").authenticate(bearer("short"), LAN, "GET", "/api/controller/state").ok)

    def test_move_runs_once_and_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_file(tmp)
            store(tmp).ensure()
            first = (Path(tmp) / "api-keys.json").read_text()
            again = store(tmp)
            self.assertTrue(again.authenticate(bearer(OLD), LAN, "GET", "/api/controller/state").ok)
            self.assertEqual((Path(tmp) / "api-keys.json").read_text(), first)

    def test_state_directory_that_cannot_be_written_keeps_the_old_token_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = ControllerTokenStore(Path(tmp) / "controller-token.json", ENV)
            keys = ApiKeyStore(Path(tmp) / "missing" / "api-keys.json", legacy)
            for path in ("/api/controller/state", "/api/controller/command"):
                self.assertTrue(keys.authenticate(bearer(ENV), LAN, "GET" if path.endswith("state") else "POST", path).ok)
            self.assertEqual(keys.mode()["store"], "legacy")
            self.assertFalse(keys.authenticate(bearer("dth_" + "x" * 43), LAN, "GET", "/api/controller/state").ok)
            with self.assertRaises(RuntimeError):
                keys.create("Kontrolrum", "read")

    def test_broken_key_file_falls_back_to_the_old_token_and_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_file(tmp)
            (Path(tmp) / "api-keys.json").write_text("{ broken")
            keys = store(tmp)
            self.assertTrue(keys.authenticate(bearer(OLD), LAN, "POST", "/api/controller/command").ok)
            self.assertEqual((Path(tmp) / "api-keys.json").read_text(), "{ broken")
            self.assertEqual(keys.list()["store"], "legacy")
            self.assertEqual(keys.list()["keys"][0]["hint"], "K7pQ")

    def test_write_that_cannot_be_read_back_is_undone(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_file(tmp)
            keys = store(tmp)
            real_write = keys._write

            def bad_write(data):
                data = json.loads(json.dumps(data))
                data["keys"][-1]["sha256"] = "0" * 64
                real_write(data)

            with mock.patch.object(keys, "_write", side_effect=bad_write):
                self.assertTrue(keys.authenticate(bearer(OLD), LAN, "GET", "/api/controller/state").ok)
            self.assertFalse((Path(tmp) / "api-keys.json").exists())
            self.assertEqual(keys.mode()["store"], "legacy")

    def test_crash_during_write_leaves_no_half_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_file(tmp)
            keys = store(tmp)
            with mock.patch("api_keys.os.replace", side_effect=OSError("disk full")):
                self.assertTrue(keys.authenticate(bearer(OLD), LAN, "POST", "/api/controller/command").ok)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["controller-token.json"])

    def test_token_made_after_a_rollback_is_moved_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_file(tmp)
            store(tmp).ensure()
            # 1.8.x ran again for a while and made a new token in its WebUI.
            newer = "newer-token-from-a-rollback-0001"
            old_file(tmp, newer)
            keys = store(tmp)
            self.assertTrue(keys.authenticate(bearer(newer), LAN, "POST", "/api/controller/command").ok)
            self.assertEqual([k["name"] for k in keys.list()["keys"]], ["Home Assistant", "Home Assistant (flyttet igen)"])

    def test_deleting_the_moved_key_removes_the_plain_text_file_and_it_never_comes_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = old_file(tmp)
            keys = store(tmp, ENV)
            moved = keys.list()["keys"][0]
            keys.delete(moved["id"])
            self.assertFalse(path.exists())
            self.assertFalse(keys.authenticate(bearer(OLD), LAN, "GET", "/api/controller/state").ok)
            restarted = store(tmp, ENV)  # the installer token is now "current" for the old code path
            self.assertFalse(restarted.authenticate(bearer(ENV), LAN, "GET", "/api/controller/state").ok)
            self.assertFalse(restarted.authenticate(bearer(OLD), LAN, "GET", "/api/controller/state").ok)
            self.assertEqual(restarted.list()["keys"], [])


class KeyTests(unittest.TestCase):
    def test_new_key_format_and_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = store(tmp)
            key, entry = keys.create("Kontrolrum", "read", user="admin")
            self.assertRegex(key, r"^dth_[A-Za-z0-9_-]{43}$")
            text = (Path(tmp) / "api-keys.json").read_text()
            self.assertNotIn(key, text)
            self.assertEqual(entry["hint"], key[-4:])
            self.assertTrue(entry["lan_only"])
            self.assertNotIn(key, json.dumps(keys.list()))
            with self.assertRaises(ValueError):
                keys.create("kontrolrum", "read")  # names are unique
            with self.assertRaises(ValueError):
                keys.create("X", "root")
            with self.assertRaises(ValueError):
                keys.create(" ", "read")
            with self.assertRaises(ValueError):
                keys.create("Y", "read", expires_days=7)

    def test_scopes_open_exactly_their_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = store(tmp)
            made = {scope: keys.create(scope, scope)[0] for scope in ("read", "control", "admin")}
            expected = {
                "read": {"/api/controller/state", "/api/controller/access-health"},
                "control": {"/api/controller/state", "/api/controller/access-health", "/api/controller/heartbeat",
                            "/api/controller/inputs", "/api/controller/signals", "/api/controller/command"},
                "admin": {path for _method, path in ENDPOINT_SCOPES},
            }
            for scope, key in made.items():
                for method, path in ENDPOINT_SCOPES:
                    result = keys.authenticate(bearer(key), LAN, method, path)
                    self.assertEqual(result.ok, path in expected[scope], (scope, path))
                    if not result.ok:
                        self.assertEqual(result.status, 403)
                # Nothing else, e.g. users or the Pi's admin actions.
                self.assertEqual(keys.authenticate(bearer(key), LAN, "POST", "/api/users/create").status, 403)
                self.assertEqual(keys.authenticate(bearer(key), LAN, "POST", "/api/admin/action").status, 403)

    def test_unknown_missing_and_expired_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            keys = store(tmp, clock=clock)
            key, _ = keys.create("Kort", "read", expires_days=30)
            self.assertEqual(keys.authenticate("", LAN, "GET", "/api/controller/state").status, 401)
            self.assertEqual(keys.authenticate(f"Basic {key}", LAN, "GET", "/api/controller/state").status, 401)
            self.assertEqual(keys.authenticate(bearer("dth_nope"), LAN, "GET", "/api/controller/state").status, 401)
            self.assertTrue(keys.authenticate(bearer(key), LAN, "GET", "/api/controller/state").ok)
            clock.now += 31 * 86400
            self.assertEqual(keys.authenticate(bearer(key), LAN, "GET", "/api/controller/state").status, 401)
            self.assertEqual(keys.rejected["reason"], "udløbet nøgle")
            self.assertEqual(keys.rejected["name"], "Kort")

    def test_home_network_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = store(tmp)
            key, _ = keys.create("Kontrolrum", "read")
            self.assertTrue(keys.authenticate(bearer(key), "192.168.1.5", "GET", "/api/controller/state").ok)
            self.assertTrue(keys.authenticate(bearer(key), "::ffff:10.0.0.9", "GET", "/api/controller/state").ok)
            outside = keys.authenticate(bearer(key), "203.0.113.7", "GET", "/api/controller/state")
            self.assertEqual((outside.ok, outside.status), (False, 403))
            tunnel = keys.authenticate(bearer(key), "127.0.0.1", "GET", "/api/controller/state", {"CF-Connecting-IP": "203.0.113.7"})
            self.assertFalse(tunnel.ok)
            anywhere, _ = keys.create("Udefra", "read", lan_only=False)
            self.assertTrue(keys.authenticate(bearer(anywhere), "203.0.113.7", "GET", "/api/controller/state").ok)
        self.assertFalse(is_home_network("100.64.1.1"))
        self.assertFalse(is_home_network("not-an-ip"))
        self.assertTrue(is_home_network("fd00::1"))

    def test_rate_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            keys = store(tmp, clock=clock)
            key, _ = keys.create("Poll", "control")
            for _ in range(api_keys.RATE_LIMITS["read"]):
                self.assertTrue(keys.authenticate(bearer(key), LAN, "GET", "/api/controller/state").ok)
            limited = keys.authenticate(bearer(key), LAN, "GET", "/api/controller/state")
            self.assertEqual(limited.status, 429)
            self.assertGreater(limited.retry_after, 0)
            # Reads and the feed are counted apart, so polling never blocks control.
            self.assertTrue(keys.authenticate(bearer(key), LAN, "POST", "/api/controller/inputs").ok)
            self.assertTrue(keys.authenticate(bearer(key), LAN, "POST", "/api/controller/command").ok)
            clock.now += 61
            self.assertTrue(keys.authenticate(bearer(key), LAN, "GET", "/api/controller/state").ok)

    def test_home_assistant_feed_rate_fits_under_the_limit(self):
        # Power and weather every 10 s, room inputs on every change, renewal every minute.
        self.assertGreaterEqual(api_keys.RATE_LIMITS["write"], 6 + 6 + 60 + 2)

    def test_wrong_keys_slow_down_an_address_but_never_a_valid_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = store(tmp, clock=Clock())
            key, _ = keys.create("HA", "control")
            codes = [keys.authenticate(bearer(f"dth_wrong{i:040d}"), LAN, "GET", "/api/controller/state").status
                     for i in range(api_keys.BAD_KEY_LIMIT + 2)]
            self.assertEqual(codes[: api_keys.BAD_KEY_LIMIT], [401] * api_keys.BAD_KEY_LIMIT)
            self.assertEqual(codes[-1], 429)
            self.assertTrue(keys.authenticate(bearer(key), LAN, "POST", "/api/controller/command").ok)

    def test_usage_is_written_at_most_once_a_minute(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            keys = store(tmp, clock=clock)
            key, entry = keys.create("HA", "control")
            usage = Path(tmp) / "api-keys-usage.json"
            keys.authenticate(bearer(key), LAN, "GET", "/api/controller/state")
            first = usage.stat().st_mtime_ns
            clock.now += 5
            with mock.patch.object(keys, "_save_usage") as save:
                keys.authenticate(bearer(key), LAN, "GET", "/api/controller/state")
                save.assert_not_called()
            self.assertEqual(usage.stat().st_mtime_ns, first)
            listed = keys.list()["keys"][0]
            self.assertEqual((listed["last_ip"], listed["uses"]), (LAN, 2))
            self.assertNotIn(key, usage.read_text())
            self.assertEqual(json.loads(usage.read_text())["keys"][entry["id"]]["uses"], 1)

    def test_log_shows_the_name_never_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = store(tmp)
            with self.assertLogs("passivelink-api-keys", level="INFO") as logs:
                key, _ = keys.create("Kontrolrum", "read")
                keys.authenticate(bearer(key), "203.0.113.7", "GET", "/api/controller/state")
                keys.authenticate(bearer(key), LAN, "POST", "/api/controller/command")
            text = "\n".join(logs.output)
            self.assertIn("Kontrolrum", text)
            self.assertNotIn(key, text)


class UpdateCheckTests(unittest.TestCase):
    def write_check(self, tmp, key, expires):
        (Path(tmp) / "update-check.json").write_text(json.dumps(
            {"sha256": hashlib.sha256(key.encode()).hexdigest(), "expires": expires, "name": "Opdateringstjek"}))

    def test_update_check_key_reads_from_this_pi_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            keys = store(tmp, clock=clock)
            check = "dth_" + "c" * 43
            self.write_check(tmp, check, clock.now + 900)
            ok = keys.authenticate(bearer(check), "127.0.0.1", "GET", "/api/controller/state")
            self.assertEqual((ok.ok, ok.name), (True, "Opdateringstjek"))
            self.assertTrue(keys.authenticate(bearer(check), "127.0.0.1", "GET", "/api/controller/access-health").ok)
            self.assertFalse(keys.authenticate(bearer(check), LAN, "GET", "/api/controller/state").ok)
            self.assertFalse(keys.authenticate(bearer(check), "127.0.0.1", "POST", "/api/controller/command").ok)
            clock.now += 901
            self.assertFalse(keys.authenticate(bearer(check), "127.0.0.1", "GET", "/api/controller/state").ok)
            self.assertEqual(keys.list()["keys"], [])  # never in the list

    def test_update_sh_no_longer_reads_a_plain_text_key(self):
        script = (ROOT / "update.sh").read_text()
        self.assertNotIn('get("token"', script)
        self.assertNotIn("read_env DANTHERM_CONTROLLER_TOKEN", script)
        self.assertIn("update-check.json", script)
        self.assertIn("/api/controller/access-health", script)
        self.assertIn("gateway/api_keys.py", script)
        # The key files are part of the root-only update backup.
        self.assertRegex(script, r"for keyfile in controller-token.json api-keys.json")

    def test_install_sh_makes_no_new_plain_text_token(self):
        script = (ROOT / "install.sh").read_text()
        self.assertNotRegex(script, r"controller_token=\$\(random_token\)")
        self.assertIn("api-keys.json", script)


class RedactionTests(unittest.TestCase):
    def test_keys_are_redacted_in_the_diagnostics_report(self):
        key = "dth_" + "A1b2" * 10 + "xyz"
        self.assertNotIn(key, redact(f"log line {key} end"))
        self.assertNotIn(key, redact(f"Authorization: Bearer {key}"))


class FakeRuntime:
    def __init__(self): self.calls = []
    def snapshot(self): return {"enabled": True, "active_master": "unknown"}
    def configure(self, patch, source="api"): self.calls.append(("config", patch, source)); return {"ok": True}
    def heartbeat(self, demand): self.calls.append(("heartbeat", demand)); return {"ha_demand": demand}
    def room_inputs(self, payload): self.calls.append(("inputs", payload)); return {"enabled": True}
    def external_signals(self, payload): self.calls.append(("signals", payload)); return {"ok": True}


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        old_file(tmp)
        self.runtime = FakeRuntime()
        self.server = ControllerDashboardHttpServer("127.0.0.1", 0, {}, "Test", None, web_root=ROOT / "gateway/webui",
                                                    history_path=tmp / "h.sqlite3", controller_runtime=self.runtime)
        self.server.auth.path = tmp / "auth.json"
        self.server.auth.session_path = tmp / "sessions.json"
        self.server.tokens.path = tmp / "controller-token.json"
        self.server.events = EventLog(tmp / "events.json")
        self.server.auth.save("admin", PASSWORD, True)
        self.server.auth.create_user("tekniker", PASSWORD, "technician")
        self.server.start()
        self.base = f"http://127.0.0.1:{self.server.server.server_address[1]}"

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def call(self, method, path, key=None, body=None):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = bearer(key)
        request = urllib.request.Request(self.base + path, method=method, headers=headers,
                                         data=json.dumps(body or {}).encode() if method == "POST" else None)
        try:
            response = urllib.request.urlopen(request)
            return response.status, json.loads(response.read() or b"null"), response.headers
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            return error.code, payload, error.headers

    def login(self, name):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        csrf = json.load(opener.open(urllib.request.Request(f"{self.base}/api/auth/login", method="POST",
            data=json.dumps({"username": name, "password": PASSWORD}).encode(), headers={"Content-Type": "application/json"})))["csrf"]
        return opener, csrf

    def admin(self, session, method, path, body=None):
        opener, csrf = session
        request = urllib.request.Request(self.base + path, method=method,
                                         data=json.dumps(body or {}).encode() if method == "POST" else None,
                                         headers={"Content-Type": "application/json", "X-CSRF-Token": csrf})
        try:
            response = opener.open(request)
            return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, None

    def test_old_token_gives_the_same_answers_after_the_update(self):
        status, state, _ = self.call("GET", "/api/controller/state", OLD)
        self.assertEqual((status, state["active_master"]), (200, "unknown"))
        self.assertEqual(self.call("POST", "/api/controller/heartbeat", OLD, {"demand": "normal"})[0], 200)
        self.assertEqual(self.call("POST", "/api/controller/command", OLD, {"mode": "auto"})[0], 200)
        self.assertEqual(self.runtime.calls[-1], ("config", {"mode": "auto"}, "home_assistant"))
        self.assertEqual(self.call("POST", "/api/controller/inputs", OLD, {"rooms": {}})[0], 200)
        self.assertEqual(self.call("POST", "/api/controller/signals", OLD, {})[0], 200)
        # The old token only had control; settings need an administrator key.
        self.assertEqual(self.call("POST", "/api/controller/config", OLD, {"mode": "auto"})[0], 403)

    def test_keys_are_managed_by_administrators_and_shown_once(self):
        tech = self.login("tekniker")
        self.assertEqual(self.admin(tech, "GET", "/api/access/keys")[0], 403)
        self.assertEqual(self.admin(tech, "POST", "/api/access/keys/create", {"name": "X", "scope": "read"})[0], 403)
        admin = self.login("admin")
        status, made = self.admin(admin, "POST", "/api/access/keys/create",
                                  {"name": "Kontrolrum", "scope": "read", "lan_only": True, "expires_days": 0})
        self.assertEqual(status, 200)
        key = made["key"]
        self.assertTrue(key.startswith("dth_"))
        status, listing = self.admin(admin, "GET", "/api/access/keys")
        self.assertNotIn(key, json.dumps(listing))
        self.assertEqual([k["name"] for k in listing["keys"]], ["Home Assistant", "Kontrolrum"])
        # Use it: header yes, URL never.
        self.assertEqual(self.call("GET", "/api/controller/state", key)[0], 200)
        self.assertNotEqual(self.call("GET", f"/api/controller/state?token={key}")[0], 200)
        self.assertNotEqual(self.call("GET", f"/api/controller/state?access_token={key}")[0], 200)
        status, body, _ = self.call("POST", "/api/controller/command", key, {"mode": "auto"})
        self.assertEqual(status, 403)
        self.assertIn("Læse og styre", body["error"])
        events = self.server.events.list()
        self.assertTrue(any("Kontrolrum" in e["text"] and e["kind"] == "token_generated" for e in events))
        self.assertNotIn(key, json.dumps(events))
        # Delete one; the others keep working.
        moved = listing["keys"][0]["id"]
        status, after = self.admin(admin, "POST", "/api/access/keys/delete", {"id": listing["keys"][1]["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", "/api/controller/state", key)[0], 401)
        self.assertEqual(self.call("GET", "/api/controller/state", OLD)[0], 200)
        self.assertEqual([k["id"] for k in after["keys"]], [moved])
        self.assertIn("token_deleted", {e["kind"] for e in self.server.events.list()})

    def test_administrator_key_can_change_settings_with_its_name_in_the_change_log(self):
        key, _ = self.server.api_keys.create("Script", "admin")
        status, _body, _ = self.call("POST", "/api/controller/config", key, {"co2_target": 900})
        self.assertEqual(status, 200)
        self.assertEqual(self.runtime.calls[-1], ("config", {"co2_target": 900}, "apikey:Script"))

    def test_wrong_key_answers(self):
        status, body, headers = self.call("POST", "/api/controller/command", "dth_" + "z" * 43, {})
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("WWW-Authenticate"), 'Bearer realm="dantherm"')
        self.assertEqual(self.call("GET", "/api/controller/state", "dth_" + "z" * 43)[0], 401)
        self.assertEqual(self.server.ha_link()["ha_link_rejected_ip"], "127.0.0.1")
        self.assertEqual(self.call("GET", "/api/controller/access-health", "dth_" + "z" * 43)[0], 401)

    def test_access_health_for_update_sh(self):
        check = "dth_" + "u" * 43
        (Path(self.tmp.name) / "update-check.json").write_text(json.dumps(
            {"sha256": hashlib.sha256(check.encode()).hexdigest(), "expires": time.time() + 600}))
        status, body, _ = self.call("GET", "/api/controller/access-health", check)
        self.assertEqual((status, body["store"], body["control_keys"]), (200, "ok", 1))
        self.assertEqual(self.call("GET", "/api/controller/state", check)[0], 200)

    def test_integration_summary_has_no_key(self):
        admin = self.login("admin")
        status, info = self.admin(admin, "GET", "/api/integration")
        self.assertEqual((status, info["configured"], info["control_keys"]), (200, True, 1))
        self.assertNotIn(OLD, json.dumps(info))
        self.assertNotIn("token", info)


if __name__ == "__main__":
    unittest.main()
