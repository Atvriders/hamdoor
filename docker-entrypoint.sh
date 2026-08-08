#!/bin/sh
# hamdoor container entrypoint.
#
# The app runs as the unprivileged `hamdoor` user (uid 10001). When /data is a
# host bind mount (e.g. - /home/james/hamdoor:/data) the directory keeps the
# host owner's permissions, so we start as root, fix ownership of /data, then
# drop privileges and exec the real command.
set -e

mkdir -p /data
if ! chown -R hamdoor:hamdoor /data 2>/dev/null; then
  echo "[hamdoor] WARNING: could not chown /data." >&2
  echo "[hamdoor] If startup fails with 'unable to open database file', run this on the host:" >&2
  echo "[hamdoor]   sudo chown -R 10001:10001 <your-data-dir>" >&2
fi

exec su -s /bin/sh hamdoor -c "$*"
