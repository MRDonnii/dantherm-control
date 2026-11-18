"""Sign-in from Kontrolrum: the handshake, the session binding and the command line."""
import hashlib
import hmac
import http.client
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "gateway"))

from controller_dashboard_server import ControllerDashboardHttpServer  # noqa: E402
from kontrolrum_sso import KontrolrumSso, RATE_LIMIT  # noqa: E402

# Linux answers on all of 127/8, so a second source address is a real other peer.
KONTROLRUM_IP = "127.0.0.1"
OTHER_IP = "127.0.0.2"


class FakeRuntime:
    def snapshot(self):
        return {"enabled": True}

    def weather_snapshot(self):
        return {}


def assertion(key, sub="kontrolrum", aud="dantherm", iat=None, lifetime=60, nonce=None, v=1):
    iat = int(time.time()) if iat is None else iat
    a = {"v": v, "aud": aud, "sub": sub, "iat": iat, "exp": iat + lifetime, "nonce": nonce or secrets.token_hex(16)}
    text = "\n".join(["kontrolrum-sso", str(a["v"]), a["aud"], a["sub"], str(a["iat"]), str(a["exp"]), a["nonce"]])
    a["sig"] = hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()
    return a


class KontrolrumSsoTests(unittest.TestCase):
    def setUp(self):
        files = self.enterContext(tempfile.TemporaryDirectory())
        self.files = Path(files)
        server = ControllerDashboardHttpServer(
            "127.0.0.1", 0, {}, "Test", None, web_root=ROOT / "gateway/webui",
            history_path=self.files / "history.sqlite3", controller_runtime=FakeRuntime(),
        )
        server.auth.path = self.files / "auth.json"
        server.auth.session_path = self.files / "webui-sessions.json"
        server.auth.save("admin", "long-test-password", True)
        server.auth.create_user("kontrolrum", "long-test-password", "admin")
        server.auth.create_user("anna", "long-test-password", "user")
        self.sso = KontrolrumSso(self.files / "kontrolrum-sso.json", "dantherm")
        server.kontrolrum_sso = self.sso
        server.start()
        self.addCleanup(server.stop)
        self.server = server
        self.port = server.server.server_address[1]
        self.key = self.sso.create_key("test")
        self.sso.set_peers([KONTROLRUM_IP])
        self.sso.set_user("kontrolrum", True)

    def call(self, method, path, body=None, cookie="", source=KONTROLRUM_IP, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10, source_address=(source, 0))
        try:
            sent = dict(headers or {})
            if cookie:
                sent["Cookie"] = cookie
            data = None
            if body is not None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                sent["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=sent)
            reply = conn.getresponse()
            raw = reply.read()
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {}
            return reply.status, parsed, reply.getheader("Set-Cookie") or ""
        finally:
            conn.close()

    def login(self, a=None, source=KONTROLRUM_IP):
        return self.call("POST", "/api/auth/kontrolrum", assertion(self.key) if a is None else a, source=source)

    def cookie(self):
        status, body, header = self.login()
        self.assertEqual(status, 200, body)
        return header.split(";", 1)[0], body["csrf"]

    def test_valid_login_gives_a_working_session_and_is_audited(self):
        cookie, csrf = self.cookie()
        status, body, _ = self.call("GET", "/api/auth/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["username"], "kontrolrum")
        self.assertEqual(body["csrf"], csrf)
        event = self.server.events.events[-1]
        self.assertEqual((event["kind"], event["user"], event["ip"]), ("login", "kontrolrum", KONTROLRUM_IP))
        self.assertEqual(self.sso.status()["last_used"]["user"], "kontrolrum")

    def test_the_session_is_not_written_to_disk(self):
        self.server.auth.session("admin", remember=True)
        before = (self.files / "webui-sessions.json").read_text()
        cookie, _ = self.cookie()
        self.assertEqual((self.files / "webui-sessions.json").read_text(), before)
        self.assertNotIn(cookie.split("=", 1)[1], before)

    def test_bad_signature(self):
        a = assertion(self.key)
        a["sig"] = "0" * 64
        self.assertEqual(self.login(a)[:2], (401, {"error": "bad_signature"}))

    def test_key_from_elsewhere_is_refused(self):
        self.assertEqual(self.login(assertion("b" * 64))[:2], (401, {"error": "bad_signature"}))

    def test_wrong_audience(self):
        self.assertEqual(self.login(assertion(self.key, aud="kioskwarden"))[:2], (401, {"error": "bad_audience"}))

    def test_wrong_version(self):
        self.assertEqual(self.login(assertion(self.key, v=2))[:2], (401, {"error": "bad_version"}))

    def test_expired(self):
        old = int(time.time()) - 600
        self.assertEqual(self.login(assertion(self.key, iat=old))[:2], (401, {"error": "expired"}))

    def test_from_the_future(self):
        ahead = int(time.time()) + 600
        self.assertEqual(self.login(assertion(self.key, iat=ahead))[:2], (401, {"error": "expired"}))

    def test_too_long_lifetime(self):
        self.assertEqual(self.login(assertion(self.key, lifetime=121))[:2], (401, {"error": "expired"}))

    def test_replay_is_refused_also_after_a_restart(self):
        a = assertion(self.key)
        self.assertEqual(self.login(a)[0], 200)
        self.assertEqual(self.login(a)[:2], (401, {"error": "replay"}))
        self.server.kontrolrum_sso = KontrolrumSso(self.files / "kontrolrum-sso.json", "dantherm")
        self.assertEqual(self.login(a)[:2], (401, {"error": "replay"}))

    def test_disabled(self):
        self.sso.disable()
        self.assertEqual(self.login(assertion(self.key))[:2], (403, {"error": "sso_disabled"}))

    def test_user_not_opted_in(self):
        self.assertEqual(self.login(assertion(self.key, sub="anna"))[:2], (401, {"error": "unknown_user"}))

    def test_opted_in_user_that_was_disabled(self):
        self.server.auth.update_user("kontrolrum", disabled=True)
        self.assertEqual(self.login()[:2], (401, {"error": "unknown_user"}))

    def test_wrong_source_ip_is_404_and_shows_the_address(self):
        self.assertEqual(self.login(source=OTHER_IP)[0], 404)
        info = self.sso.status()
        self.assertEqual((info["rejected_ip"], info["rejected_reason"]), (OTHER_IP, "wrong_peer"))

    def test_no_configured_address_refuses_everyone(self):
        self.sso.set_peers([])
        self.assertEqual(self.login()[0], 404)

    def test_copied_cookie_does_not_work_from_another_address(self):
        cookie, _ = self.cookie()
        self.sso.set_peers([KONTROLRUM_IP, OTHER_IP])
        self.assertEqual(self.call("GET", "/api/users", cookie=cookie, source=OTHER_IP)[0], 401)
        self.assertFalse(self.call("GET", "/api/auth/status", cookie=cookie, source=OTHER_IP)[1]["authenticated"])
        self.assertTrue(self.call("GET", "/api/auth/status", cookie=cookie)[1]["authenticated"], "still valid at home")

    def test_new_key_off_and_removed_tick_end_the_session(self):
        for end in (lambda: self.sso.create_key("test"), self.sso.disable,
                    lambda: self.sso.set_user("kontrolrum", False)):
            self.key = self.sso.key() or self.sso.create_key("test")
            self.sso.set_user("kontrolrum", True)
            cookie, _ = self.cookie()
            end()
            self.assertFalse(self.call("GET", "/api/auth/status", cookie=cookie)[1]["authenticated"])

    def test_rate_limit(self):
        replies = [self.login(assertion("b" * 64))[0] for _ in range(RATE_LIMIT)]
        self.assertEqual(set(replies), {401})
        self.assertEqual(self.login()[:2], (429, {"error": "rate_limited"}))

    def test_rubbish_is_a_clean_refusal(self):
        for body in (b"", b"{", b"[1]", b"\xff\xfe", json.dumps({"v": True}).encode(), b"x" * 5000):
            self.sso._hits.clear()
            self.assertEqual(self.login(body)[0], 400, body[:20])

    def test_a_fault_in_the_sign_in_code_only_refuses_this_request(self):
        def broken(*args):
            raise RuntimeError("boom")
        self.sso.login = broken
        self.assertEqual(self.login()[:2], (503, {"error": "unavailable"}))
        status, body, _ = self.call("POST", "/api/auth/login", {"username": "admin", "password": "long-test-password"})
        self.assertEqual((status, body["ok"]), (200, True))

    def test_without_the_module_the_endpoint_is_404_and_passwords_still_work(self):
        self.server.kontrolrum_sso = None
        self.assertEqual(self.login()[0], 404)
        self.assertEqual(self.call("POST", "/api/auth/login", {"username": "admin", "password": "long-test-password"})[0], 200)

    def test_unreadable_settings_file_means_off(self):
        (self.files / "kontrolrum-sso.json").write_text("{not json")
        self.assertEqual(self.login()[0], 404)
        self.assertFalse(self.sso.status()["enabled"])

    def test_settings_need_an_administrator_and_never_show_the_key(self):
        self.assertEqual(self.call("GET", "/api/auth/kontrolrum/status")[0], 401)
        _, body, header = self.call("POST", "/api/auth/login", {"username": "anna", "password": "long-test-password"})
        anna = {"cookie": header.split(";", 1)[0], "csrf": body["csrf"]}
        self.assertEqual(self.call("GET", "/api/auth/kontrolrum/status", cookie=anna["cookie"])[0], 403)
        self.assertEqual(self.call("POST", "/api/auth/kontrolrum/key", {}, cookie=anna["cookie"],
                                   headers={"X-CSRF-Token": anna["csrf"]})[0], 403)
        _, body, header = self.call("POST", "/api/auth/login", {"username": "admin", "password": "long-test-password"})
        cookie = header.split(";", 1)[0]
        status, info, _ = self.call("GET", "/api/auth/kontrolrum/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertNotIn(self.key, json.dumps(info))

    def test_the_files_are_private(self):
        self.login()
        for name in ("kontrolrum-sso.json", "kontrolrum-sso-nonces.json", "kontrolrum-sso-status.json"):
            self.assertEqual((self.files / name).stat().st_mode & 0o777, 0o600, name)


class CommandLineTests(unittest.TestCase):
    def run_cli(self, folder, *args):
        env = dict(os.environ, DANTHERM_WEBUI_AUTH_FILE=str(Path(folder) / "webui-auth.json"))
        return subprocess.run([sys.executable, str(ROOT / "gateway/kontrolrum_sso.py"), *args],
                              capture_output=True, text=True, env=env, timeout=30, check=False)

    def test_pairing_prints_only_the_key_and_ticks_the_user(self):
        from webui_auth import AuthManager
        with tempfile.TemporaryDirectory() as folder:
            AuthManager(Path(folder) / "webui-auth.json").save("kontrolrum", "long-test-password", True)
            done = self.run_cli(folder, "kontrolrum-sso", "kontrolrum", "--peer", "10.0.0.28")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertRegex(done.stdout, r"\A[0-9a-f]{64}\Z")
            self.assertNotIn(done.stdout, done.stderr)
            sso = KontrolrumSso(Path(folder) / "kontrolrum-sso.json", "dantherm")
            self.assertEqual(sso.key(), done.stdout)
            self.assertEqual((sso.users(), sso.peers()), (["kontrolrum"], ["10.0.0.28"]))
            status = self.run_cli(folder, "status")
            self.assertNotIn(done.stdout, status.stdout + status.stderr)
            self.assertEqual(self.run_cli(folder, "off").returncode, 0)
            self.assertEqual(sso.key(), "")

    def test_unknown_user_makes_no_key(self):
        from webui_auth import AuthManager
        with tempfile.TemporaryDirectory() as folder:
            AuthManager(Path(folder) / "webui-auth.json").save("kontrolrum", "long-test-password", True)
            done = self.run_cli(folder, "kontrolrum-sso", "nobody")
            self.assertEqual((done.returncode, done.stdout), (1, ""))
            self.assertFalse((Path(folder) / "kontrolrum-sso.json").exists())
