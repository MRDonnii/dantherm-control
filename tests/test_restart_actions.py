"""Restart buttons: the admin helper's allowlisted restart/reboot actions and
the WebUI proxy in front of it. Nothing is ever restarted for real:
subprocess.run (systemctl) only records, and the proxy talks to a fake helper."""

import http.cookiejar
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
os.environ.setdefault("DANTHERM_REBOOT_TOKEN", "test-token")
spec = importlib.util.spec_from_file_location("restart_actions_admin_test", ROOT / "gateway/dantherm_pi_admin_api.py")
admin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(admin)

from controller_dashboard_server import ControllerDashboardHttpServer  # noqa: E402
from event_log import EventLog  # noqa: E402

PASSWORD = "long-test-password"


def wait_for(mock_run, seconds=4.0):
    deadline = time.monotonic() + seconds
    while not mock_run.called and time.monotonic() < deadline:
        time.sleep(0.05)


class AdminHelperRestartTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        # systemctl is never called for real: subprocess.run only records.
        self.patch = mock.patch.object(admin.subprocess, "run")
        self.run = self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.server.shutdown(); self.server.server_close()

    def post(self, payload, token=admin.TOKEN):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(f"{self.base}/action", data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def test_restart_all_restarts_the_control_services_in_one_call_after_replying(self):
        status, reply = self.post({"action": "restart_service", "target": "all"})
        self.assertEqual(status, 202)
        self.assertTrue(reply["ok"])
        self.run.assert_not_called()  # only scheduled; runs after the reply
        wait_for(self.run)
        self.run.assert_called_once_with(["systemctl", "restart", *admin.SERVICES.values()], check=False)
        self.assertNotIn(admin.ADMIN_SERVICE, self.run.call_args.args[0])

    def test_reboot_is_scheduled_not_run_inline(self):
        status, _ = self.post({"action": "reboot"})
        self.assertEqual(status, 202)
        self.run.assert_not_called()
        wait_for(self.run)
        self.run.assert_called_once_with(["systemctl", "reboot"], check=False)

    def test_unknown_service_and_missing_token_are_refused(self):
        self.assertEqual(self.post({"action": "restart_service", "target": "ssh"})[0], 400)
        self.assertEqual(self.post({"action": "restart_service", "target": "all"}, token=None)[0], 401)
        self.assertEqual(self.post({"action": "reboot"}, token="wrong")[0], 401)
        time.sleep(0.8)
        self.run.assert_not_called()


class FakeAdminHelper(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        FakeAdminHelper.received.append((self.headers.get("Authorization"), json.loads(self.rfile.read(length))))
        body = json.dumps({"ok": True, "message": "scheduled"}).encode()
        self.send_response(202); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeRuntime:
    def snapshot(self): return {"enabled": True}
    def configure(self, patch, source="api"): return patch


class WebUiRestartProxyTests(unittest.TestCase):
    def test_only_system_roles_with_csrf_reach_the_helper_and_the_restart_is_logged(self):
        FakeAdminHelper.received = []
        helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeAdminHelper)
        threading.Thread(target=helper.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as tmp:
            server = ControllerDashboardHttpServer("127.0.0.1", 0, {}, "Test", None, web_root=ROOT / "gateway/webui",
                                                   history_path=Path(tmp) / "history.sqlite3", controller_runtime=FakeRuntime())
            server.auth.path = Path(tmp) / "auth.json"; server.auth.session_path = Path(tmp) / "sessions.json"
            server.events = EventLog(Path(tmp) / "events.json")
            server.admin_url = f"http://127.0.0.1:{helper.server_address[1]}"; server.admin_token = "helper-token"
            server.auth.save("admin", PASSWORD, True)
            server.start(); base = f"http://127.0.0.1:{server.server.server_address[1]}"

            def login(name):
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                reply = json.load(opener.open(urllib.request.Request(f"{base}/api/auth/login", method="POST",
                    data=json.dumps({"username": name, "password": PASSWORD}).encode(), headers={"Content-Type": "application/json"})))
                return opener, reply["csrf"]

            def post(session, payload, csrf=None):
                opener, token = session
                request = urllib.request.Request(f"{base}/api/admin/action", data=json.dumps(payload).encode(), method="POST",
                                                 headers={"Content-Type": "application/json", "X-CSRF-Token": token if csrf is None else csrf})
                try:
                    return opener.open(request).status
                except urllib.error.HTTPError as error:
                    return error.code

            try:
                owner = login("admin")
                for name, role in (("tekniker", "technician"), ("bruger", "user")):
                    request = urllib.request.Request(f"{base}/api/users/create", method="POST", headers={"Content-Type": "application/json", "X-CSRF-Token": owner[1]},
                                                     data=json.dumps({"username": name, "password": PASSWORD, "role": role}).encode())
                    self.assertEqual(owner[0].open(request).status, 200)
                user = login("bruger")
                self.assertEqual(post(user, {"action": "restart_service", "target": "all"}), 403)
                self.assertEqual(post(user, {"action": "reboot"}), 403)
                self.assertEqual(post(owner, {"action": "reboot"}, csrf="forged"), 403)
                self.assertEqual(FakeAdminHelper.received, [])

                self.assertEqual(post(owner, {"action": "restart_service", "target": "all"}), 202)
                self.assertEqual(post(login("tekniker"), {"action": "reboot"}), 202)
                self.assertEqual([body for _, body in FakeAdminHelper.received],
                                 [{"action": "restart_service", "target": "all"}, {"action": "reboot"}])
                self.assertTrue(all(auth == "Bearer helper-token" for auth, _ in FakeAdminHelper.received))
                events = [e for e in json.load(owner[0].open(f"{base}/api/events"))["events"] if e["kind"] == "system_restart"]
                self.assertEqual(sorted(e["by"] for e in events), ["admin", "tekniker"])
            finally:
                server.stop(); helper.shutdown(); helper.server_close()


if __name__ == "__main__":
    unittest.main()
