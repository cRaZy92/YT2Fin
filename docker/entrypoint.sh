#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ] && [ "${YTJS_PRIVS_DROPPED:-0}" != "1" ]; then
  export YTJS_PRIVS_DROPPED=1
  groupadd -o -g "$PGID" app 2>/dev/null || true
  useradd -o -u "$PUID" -g "$PGID" -M -d /config -s /usr/sbin/nologin app 2>/dev/null || true
  mkdir -p /config /media
  chown -R "$PUID:$PGID" /config
  # Docker creates a missing bind-mount folder as root:root, which the app user can't write.
  # Claim it only when it's empty; never touch ownership of an existing library.
  if ! gosu "$PUID:$PGID" test -w /media; then
    if [ -z "$(ls -A /media)" ]; then
      echo "/media is empty and not writable by $PUID:$PGID; taking ownership of it"
      chown "$PUID:$PGID" /media
    else
      echo "WARNING: /media is not writable by $PUID:$PGID; fix its owner or permissions (the web UI shows this too)" >&2
    fi
  fi
  # The app updates yt-dlp in place, so the venv must be writable by the runtime user.
  if [ "$(stat -c %u /opt/venv)" != "$PUID" ]; then
    chown -R "$PUID:$PGID" /opt/venv
  fi
  exec gosu "$PUID:$PGID" "$0" "$@"
fi

umask 002
stopping=0
child=0
term() {
  stopping=1
  [ "$child" -ne 0 ] && kill -TERM "$child" 2>/dev/null || true
}
trap term TERM INT

while :; do
  python -m app &
  child=$!
  set +e
  wait "$child"
  code=$?
  if [ "$stopping" = 1 ]; then
    wait "$child" 2>/dev/null
    exit 0
  fi
  set -e
  if [ "$code" -ne 3 ]; then
    exit "$code"
  fi
  echo "yt-dlp was updated; restarting the app..."
done
