"""update.sh keeps the leftover NFS-root watchdog from running on an SD/disk boot.

The guard runs against a temporary systemd directory and a fake systemctl that
only records its calls; nothing on this machine is started or stopped."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts/netboot/hch-nfs-watchdog-guard.sh"
HELPER = ROOT / "scripts/netboot/hch-root-is-nfs"
UNIT = "hch5-nfs-watchdog.service"

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
  cat) [ "$FAKE_UNIT_EXISTS" = 1 ] ;;
  is-active) [ "$FAKE_ACTIVE" = 1 ] ;;
  *) exit 0 ;;
esac
"""

SD_MOUNTS = "/dev/mmcblk0p2 / ext4 rw,noatime 0 0\nproc /proc proc rw 0 0\n"
NFS_MOUNTS = "rootfs / rootfs rw 0 0\n10.0.0.10:/mnt/user/pi / nfs rw,vers=3 0 0\n"


class NfsWatchdogGuardTests(unittest.TestCase):
    def run_guard(self, *, exists, active, mounts):
        tmp = Path(self._tmp.name)
        (tmp / "mounts").write_text(mounts)
        env = {
            **os.environ,
            "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}",
            "FAKE_LOG": str(tmp / "systemctl.log"),
            "FAKE_UNIT_EXISTS": "1" if exists else "0",
            "FAKE_ACTIVE": "1" if active else "0",
            "HCH_SYSTEMD_DIR": str(tmp / "systemd"),
            "HCH_ROOT_IS_NFS": str(tmp / "sbin/hch-root-is-nfs"),
            "HCH_MOUNTS_FILE": str(tmp / "mounts"),
        }
        result = subprocess.run(["bash", str(GUARD), str(HELPER)], env=env, capture_output=True, text=True, timeout=20)
        log = (tmp / "systemctl.log").read_text().splitlines() if (tmp / "systemctl.log").exists() else []
        return result, log

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        (tmp / "bin").mkdir()
        (tmp / "bin/systemctl").write_text(FAKE_SYSTEMCTL)
        (tmp / "bin/systemctl").chmod(0o755)
        self.dropin = tmp / "systemd" / f"{UNIT}.d" / "50-only-on-nfs-root.conf"
        self.helper = tmp / "sbin/hch-root-is-nfs"

    def tearDown(self):
        self._tmp.cleanup()

    def test_running_watchdog_on_sd_gets_condition_and_is_stopped(self):
        result, log = self.run_guard(exists=True, active=True, mounts=SD_MOUNTS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dropin.read_text(), f"# Installed by HCH5 Control update.sh: only run while / is mounted over NFS.\n[Service]\nExecCondition={self.helper}\n")
        self.assertTrue(os.access(self.helper, os.X_OK))
        self.assertIn("daemon-reload", log)
        self.assertIn(f"stop {UNIT}", log)
        self.assertFalse(any(line.startswith(("enable", "disable", "mask", "start", "restart")) for line in log), log)

    def test_on_nfs_root_the_watchdog_keeps_running(self):
        result, log = self.run_guard(exists=True, active=True, mounts=NFS_MOUNTS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.dropin.exists())
        self.assertNotIn(f"stop {UNIT}", log)
        self.assertIn("/ is on NFS", result.stdout)

    def test_missing_unit_is_not_touched(self):
        result, log = self.run_guard(exists=False, active=False, mounts=SD_MOUNTS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log, [f"cat {UNIT}"])
        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.helper.exists())

    def test_second_run_does_not_reload_again_and_stopped_unit_is_left_alone(self):
        self.run_guard(exists=True, active=True, mounts=SD_MOUNTS)
        (Path(self._tmp.name) / "systemctl.log").unlink()
        result, log = self.run_guard(exists=True, active=False, mounts=SD_MOUNTS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("daemon-reload", log)
        self.assertNotIn(f"stop {UNIT}", log)

    def test_update_sh_runs_the_guard_only_after_the_update_was_accepted(self):
        script = (ROOT / "update.sh").read_text()
        accepted = script.index("trap - ERR")
        guard = script.index("hch-nfs-watchdog-guard.sh")
        self.assertGreater(guard, accepted)
        self.assertIn("|| echo \"NFS watchdog guard could not be applied", script)

    def test_update_sh_restarts_the_admin_helper_only_after_a_manual_update(self):
        script = (ROOT / "update.sh").read_text()
        self.assertIn('if [[ ${HCH5_UPDATE_FROM_ADMIN:-0} != 1 ]] && systemctl is-active --quiet dantherm-webui-admin.service; then', script)
        self.assertGreater(script.index("systemctl restart dantherm-webui-admin.service"), script.index("trap - ERR"))


if __name__ == "__main__":
    unittest.main()
