#!/bin/sh
set -e
# Fix volume ownership — setup.sh creates volumes as root; the scarguard
# user needs read access to /data (snapshots) and write access to the
# species DB.  Sentinel file prevents slow restarts.
if [ "$(id -u)" = "0" ]; then
    if [ ! -f /tmp/.ownership-fixed-speciesnet ]; then
        chown scarguard:scarguard /data /config 2>/dev/null || echo "WARNING: chown failed on /data or /config — check volume mounts" >&2
        touch /tmp/.ownership-fixed-speciesnet 2>/dev/null || true
    fi
    exec gosu scarguard "$@"
fi
exec "$@"
