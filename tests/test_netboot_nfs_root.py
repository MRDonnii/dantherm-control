"""The netboot-only helpers must act only when / is really mounted over NFS."""
import os, shutil, stat, subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "scripts/netboot/hch-root-is-nfs"
PREPARE = ROOT / "scripts/netboot/hch-nm-netboot-prepare.sh"

NFS_ROOT = "sysfs /sys sysfs rw 0 0\nserver:/srv/root / nfs rw,vers=3,addr=192.0.2.1 0 0\nproc /proc proc rw 0 0\n"
NFS4_ROOT = "server:/root / nfs4 rw,vers=4.2 0 0\n"
OLD_KERNEL_NFS = "rootfs / rootfs rw 0 0\nserver:/srv/root / nfs rw 0 0\n"
SD_ROOT = "/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n/dev/mmcblk0p1 /boot/firmware vfat rw 0 0\n"
DISK_ROOT = "/dev/sda2 / ext4 rw 0 0\n"
NFS_ELSEWHERE = "/dev/mmcblk0p2 / ext4 rw 0 0\nserver:/backup /mnt/backup nfs rw 0 0\n"
NFS_SUBDIR = "/dev/sda2 / ext4 rw 0 0\nserver:/data /data nfs4 rw 0 0\n"

CASES = [
    (NFS_ROOT, True), (NFS4_ROOT, True), (OLD_KERNEL_NFS, True),
    (SD_ROOT, False), (DISK_ROOT, False), (NFS_ELSEWHERE, False), (NFS_SUBDIR, False), ("", False),
]


def _mounts(tmp_path, text):
    path = tmp_path / "mounts"
    path.write_text(text)
    return path


@pytest.mark.parametrize("text,is_nfs", CASES)
def test_helper_reports_only_an_nfs_root(tmp_path, text, is_nfs):
    result = subprocess.run(["sh", str(HELPER)], env={**os.environ, "HCH_MOUNTS_FILE": str(_mounts(tmp_path, text))},
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is is_nfs
    assert result.stdout == "" and result.stderr == ""  # never warns


def test_helper_is_silent_and_negative_without_a_mounts_file(tmp_path):
    result = subprocess.run(["sh", str(HELPER)], env={**os.environ, "HCH_MOUNTS_FILE": str(tmp_path / "missing")},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode != 0 and result.stdout == "" and result.stderr == ""


def test_helper_is_executable():
    assert HELPER.stat().st_mode & stat.S_IXUSR


def _run_prepare(tmp_path, text):
    """Run the prepare script against temporary directories and a fake mount."""
    plugin_dir, profile_dir, runtime_dir = tmp_path / "plugins", tmp_path / "profiles", tmp_path / "run"
    plugin_dir.mkdir(); profile_dir.mkdir()
    (profile_dir / "home.nmconnection").write_text("[connection]\n")
    script = PREPARE.read_text()
    for old, new in (("/usr/lib/arm-linux-gnueabihf/NetworkManager/1.42.4", plugin_dir),
                     ("/etc/NetworkManager/system-connections", profile_dir),
                     ("/run/hch-nm-netboot", runtime_dir),
                     ("/var/lib/dantherm-admin/nm-connections", tmp_path / "saved")):
        assert old in script
        script = script.replace(old, str(new))
    copy = tmp_path / "prepare.sh"; copy.write_text(script)
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "mount.log"
    fake_mount = bin_dir / "mount"
    fake_mount.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n'); fake_mount.chmod(0o755)
    env = {**os.environ, "HCH_MOUNTS_FILE": str(_mounts(tmp_path, text)), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run(["sh", str(copy)], env=env, capture_output=True, text=True, timeout=10)
    return result, runtime_dir, log


@pytest.mark.parametrize("text", [SD_ROOT, DISK_ROOT, NFS_ELSEWHERE])
def test_prepare_leaves_a_local_root_alone(tmp_path, text):
    result, runtime_dir, log = _run_prepare(tmp_path, text)
    assert result.returncode == 0 and result.stdout == "" and result.stderr == ""
    assert not runtime_dir.exists() and not log.exists()


@pytest.mark.parametrize("text", [NFS_ROOT, NFS4_ROOT])
def test_prepare_still_moves_networkmanager_files_to_ram_on_nfs(tmp_path, text):
    result, runtime_dir, log = _run_prepare(tmp_path, text)
    assert result.returncode == 0, result.stderr
    assert (runtime_dir / "connections/home.nmconnection").exists()
    assert log.read_text().count("--bind") == 2


def test_prepare_and_helper_use_the_same_nfs_test():
    check = "awk '$2 == \"/\" { fs = $3 } END { exit !(fs ~ /^nfs4?$/) }'"
    assert check in HELPER.read_text() and check in PREPARE.read_text()
