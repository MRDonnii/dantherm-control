#!/usr/bin/env bash
# Keep the NFS-root watchdog from running when the Pi boots from SD/disk.
#
# hch5-nfs-watchdog.service is not part of this repository: it was left on the
# Pi by the old netboot setup and can still be running after the move to an
# SD card. When the unit exists, this installs hch-root-is-nfs and a drop-in
# with ExecCondition=, so systemd skips the watchdog unless / is on NFS, and
# stops it now if the root is not NFS. When the unit does not exist, nothing
# is touched. It never enables, disables or deletes anything.
#
# Usage: hch-nfs-watchdog-guard.sh <path to hch-root-is-nfs in the source>
# Overrides for tests: HCH_NFS_WATCHDOG_UNIT, HCH_SYSTEMD_DIR, HCH_ROOT_IS_NFS
# (installed path of the helper) and HCH_MOUNTS_FILE (read by the helper).
set -u

source_helper=${1:?usage: hch-nfs-watchdog-guard.sh <hch-root-is-nfs>}
unit=${HCH_NFS_WATCHDOG_UNIT:-hch5-nfs-watchdog.service}
unit_dir=${HCH_SYSTEMD_DIR:-/etc/systemd/system}
helper=${HCH_ROOT_IS_NFS:-/usr/local/sbin/hch-root-is-nfs}
dropin_dir=${unit_dir}/${unit}.d
dropin=${dropin_dir}/50-only-on-nfs-root.conf

if ! systemctl cat "${unit}" >/dev/null 2>&1; then
  echo "NFS watchdog: ${unit} not installed; nothing to do."
  exit 0
fi

install -d -m 0755 "$(dirname -- "${helper}")"
install -m 0755 "${source_helper}" "${helper}"

wanted=$(printf '%s\n' \
  "# Installed by HCH5 Control update.sh: only run while / is mounted over NFS." \
  "[Service]" \
  "ExecCondition=${helper}")
if [[ ! -f ${dropin} || $(cat "${dropin}") != "${wanted}" ]]; then
  install -d -m 0755 "${dropin_dir}"
  printf '%s\n' "${wanted}" > "${dropin}.tmp" && mv -f "${dropin}.tmp" "${dropin}"
  chmod 0644 "${dropin}"
  systemctl daemon-reload
  echo "NFS watchdog: ${unit} now only starts when / is on NFS (${dropin})."
fi

if "${helper}"; then
  echo "NFS watchdog: / is on NFS; ${unit} left as it is."
elif systemctl is-active --quiet "${unit}"; then
  systemctl stop "${unit}"
  echo "NFS watchdog: / is not on NFS; stopped ${unit}."
else
  echo "NFS watchdog: / is not on NFS; ${unit} is not running."
fi
exit 0
