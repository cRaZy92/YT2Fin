#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
  groupadd -o -g "$PGID" app 2>/dev/null || true
  useradd -o -u "$PUID" -g "$PGID" -M -d /config -s /usr/sbin/nologin app 2>/dev/null || true
  mkdir -p /config /media
  chown "$PUID:$PGID" /config
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
