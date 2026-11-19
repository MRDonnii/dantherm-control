"""The update page's failsafe status: a failed attempt is explained by the end of
the update.sh log, is cleared once a newer or equal version runs, can be hidden,
survives a restart of the admin helper, and an update never installs a build
that is not newer than the running one. update.sh here is a tiny test script."""

import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
os.environ.setdefault("DANTHERM_REBOOT_TOKEN", "test-token")
spec = importlib.util.spec_from_file_location("update_status_admin_test", ROOT / "gateway/dantherm_pi_admin_api.py")
admin = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(admin)

FAILING_UPDATER = """#!/usr/bin/env bash
echo "[1/6] Validating staged Python files..."
echo "from admin: ${HCH5_UPDATE_FROM_ADMIN:-no}"
echo "Saved RS485 device is currently unavailable: /dev/serial/by-id/usb-test. Live service is left untouched." >&2
exit 2
"""


def make_archive(path: Path, root: str, updater: str) -> None:
    data = updater.encode()
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(f"{root}/update.sh"); info.size = len(data); info.mode = 0o755
        tar.addfile(info, io.BytesIO(data))


class UpdateStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saved_state = dict(admin.UPDATE_STATE)
        self.version = "1.6.1"
        self.archive = self.dir / "release.tar.gz"
        make_archive(self.archive, "dantherm-control-1.7.0", FAILING_UPDATER)
        self.patches = [
            mock.patch.object(admin, "UPDATE_STATE_FILE", self.dir / "update-state.json"),
            mock.patch.object(admin, "UPDATE_LOG_FILE", self.dir / "update-last.log"),
            mock.patch.object(admin, "UPDATE_CHANNEL_FILE", self.dir / "update-channel"),
            mock.patch.object(admin, "current_version", side_effect=lambda: self.version),
            mock.patch.object(admin, "_schedule_admin_restart"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        admin.UPDATE_STATE.clear(); admin.UPDATE_STATE.update(self.saved_state)
        self.tmp.cleanup()

    def offer(self, version):
        return {"ref": f"v{version}", "available_version": version, "available_build": version}

    def fail_an_update_to_170(self):
        with mock.patch.object(admin, "update_info", return_value=self.offer("1.7.0")), \
                mock.patch.object(admin, "_download_tarball", side_effect=lambda ref, dest: dest.write_bytes(self.archive.read_bytes())):
            admin._install_update("beta")

    def test_a_failed_attempt_keeps_time_versions_and_the_end_of_the_log(self):
        self.fail_an_update_to_170()
        state = admin.update_state_view()
        self.assertEqual(state["phase"], "failed")
        self.assertFalse(state["running"])
        self.assertEqual(state["last_error"], "update.sh stoppede med kode 2")
        self.assertEqual((state["from_version"], state["target_version"], state["ref"], state["channel"]), ("1.6.1", "1.7.0", "v1.7.0", "beta"))
        self.assertIsInstance(state["started_at"], float)
        self.assertIsInstance(state["finished_at"], float)
        self.assertIn("Saved RS485 device is currently unavailable", state["log_tail"])
        self.assertIn("from admin: 1", state["log_tail"])  # update.sh must not restart its parent
        log = (self.dir / "update-last.log").read_text()
        self.assertIn("kanal beta · installeret 1.6.1", log)
        self.assertIn("FEJL: update.sh stoppede med kode 2", log)
        saved = json.loads((self.dir / "update-state.json").read_text())
        self.assertEqual((saved["phase"], saved["target_version"]), ("failed", "1.7.0"))

    def test_the_failure_clears_itself_once_the_target_or_newer_runs(self):
        self.fail_an_update_to_170()
        self.version = "1.6.9"
        self.assertEqual(admin.update_state_view()["phase"], "failed")
        self.version = "1.7.5"  # e.g. a manual update.sh later
        state = admin.update_state_view()
        self.assertEqual(state["phase"], "dismissed")
        self.assertIsNone(state["last_error"])
        self.assertIsNone(state["log_tail"])
        self.assertEqual((state["result"], state["target_version"]), ("failed", "1.7.0"))  # the attempt stays on record
        self.assertIn("1.7.5", state["detail"])

    def test_never_installs_a_build_that_is_not_newer(self):
        self.version = "1.7.5"
        with mock.patch.object(admin, "update_info", return_value=self.offer("1.7.0")), \
                mock.patch.object(admin, "_download_tarball") as download, mock.patch.object(admin.subprocess, "run") as run:
            admin._install_update("beta")
        download.assert_not_called(); run.assert_not_called()
        state = admin.update_state_view()
        self.assertEqual(state["phase"], "up_to_date")
        self.assertIsNone(state["last_error"])
        self.assertIn("1.7.5 kører, kanalen tilbyder 1.7.0", state["detail"])

    def test_state_survives_a_helper_restart_and_an_interrupted_attempt_reads_as_failed(self):
        self.fail_an_update_to_170()
        admin.UPDATE_STATE.update(phase="idle", last_error=None, log_tail=None, target_version=None)
        admin.load_update_state()
        self.assertEqual(admin.UPDATE_STATE["phase"], "failed")
        self.assertIn("RS485", admin.UPDATE_STATE["log_tail"])
        (self.dir / "update-state.json").write_text(json.dumps({"phase": "installing", "progress": 58, "target_version": "1.7.4"}))
        admin.load_update_state()
        self.assertEqual(admin.UPDATE_STATE["phase"], "failed")
        self.assertFalse(admin.UPDATE_STATE["running"])
        self.assertIn("Afbrudt", admin.UPDATE_STATE["last_error"])

    def test_log_is_rotated_and_kept_under_the_size_limit(self):
        self.fail_an_update_to_170()
        self.fail_an_update_to_170()
        self.assertTrue((self.dir / "update-last.log.1").exists())
        (self.dir / "update-last.log").write_text("x" * 99 + "\n" + ("line\n" * 60_000))
        admin._cap_update_log()
        data = (self.dir / "update-last.log").read_bytes()
        self.assertLessEqual(len(data), admin.UPDATE_LOG_MAX_BYTES + 64)
        self.assertTrue(data.startswith("[... ældre linjer fjernet ...]\n".encode()))

    def test_dismiss_over_http_hides_the_error(self):
        self.fail_an_update_to_170()
        server = ThreadingHTTPServer(("127.0.0.1", 0), admin.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/action", method="POST",
                                             data=json.dumps({"action": "dismiss_update_error"}).encode(),
                                             headers={"Authorization": f"Bearer {admin.TOKEN}", "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                reply = json.load(response)
        finally:
            server.shutdown(); server.server_close()
        self.assertEqual(reply["update"]["phase"], "dismissed")
        self.assertIsNone(reply["update"]["last_error"])
        self.assertEqual(reply["update"]["target_version"], "1.7.0")

    def test_beta_channel_picks_the_newest_tag_not_an_old_prerelease(self):
        feed = "<entry><link href='/releases/tag/v1.7.5'/></entry><entry><link href='/releases/tag/v1.7.0'/></entry>" \
               "<entry><id>tag:github.com,2008:Repository/1/v1.6.1</id></entry><entry>v1.7.4</entry>"
        with mock.patch.object(admin, "_request_text", return_value=feed):
            self.assertEqual(admin._latest_beta_tag(), "v1.7.5")


if __name__ == "__main__":
    unittest.main()
