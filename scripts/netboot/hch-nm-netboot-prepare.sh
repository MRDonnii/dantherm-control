#!/bin/sh
set -eu
plugin_dir=/usr/lib/arm-linux-gnueabihf/NetworkManager/1.42.4
profile_dir=/etc/NetworkManager/system-connections
runtime_dir=/run/hch-nm-netboot
# Only an NFS root needs RAM-backed NetworkManager files. After a move to a
# local SD card or disk, leave NetworkManager's normal persistent storage alone.
mounts=${HCH_MOUNTS_FILE:-/proc/mounts}
awk '$2 == "/" { fs = $3 } END { exit !(fs ~ /^nfs4?$/) }' "$mounts" 2>/dev/null || exit 0
[ -d "$plugin_dir" ] || exit 0
[ -d "$profile_dir" ] || exit 0
mkdir -p "$runtime_dir/plugins" "$runtime_dir/connections"
chmod 700 "$runtime_dir/connections"
mount -t tmpfs -o size=8m,mode=755,exec tmpfs "$runtime_dir/plugins"
cp -a "$plugin_dir"/. "$runtime_dir/plugins"/
cp -a "$profile_dir"/. "$runtime_dir/connections"/
if [ -d /var/lib/dantherm-admin/nm-connections ]; then
    cp -a /var/lib/dantherm-admin/nm-connections/. "$runtime_dir/connections"/
fi
mount --bind "$runtime_dir/plugins" "$plugin_dir"
mount --bind "$runtime_dir/connections" "$profile_dir"
