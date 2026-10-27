#!/usr/bin/env bash
# Disposable Postgres + the app + headless Chromium, as an unprivileged user.
# Exit 0: the page total is 600. Exit 1: anything else. Always tears down.
set -euo pipefail
cd "$(dirname "$0")"

TMP="${TMPDIR:-/tmp}"
PGDATA="$TMP/demo-pg"
PGPORT=5433
APP_URL="http://127.0.0.1:8088/"
VENV="${DEMO_VENV:-/tmp/venv}"
DOM="$TMP/demo-dom.html"
SHOT="$TMP/demo-shot.png"
APP_LOG="$TMP/demo-app.log"
CHROME_PROFILE="$TMP/demo-chrome"
APP_PID=""

PGBIN=""
for d in /usr/lib/postgresql/*/bin; do
    if [ -x "$d/pg_ctl" ]; then PGBIN="$d"; break; fi
done
if [ -z "$PGBIN" ]; then
    PGBIN="$(dirname "$(command -v pg_ctl)")"
fi

cleanup() {
    if [ -n "$APP_PID" ]; then kill "$APP_PID" 2>/dev/null || true; rm -f "$APP_PIDFILE"; fi
    "$PGBIN/pg_ctl" -D "$PGDATA" stop -m fast >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Stop instances a previous run of this script left behind (an agent may have killed
# the script before its trap ran), then refuse to test against a stale server.
APP_PIDFILE="$TMP/demo-app.pid"
if [ -f "$APP_PIDFILE" ]; then kill "$(cat "$APP_PIDFILE")" 2>/dev/null || true; rm -f "$APP_PIDFILE"; fi
if [ -f "$PGDATA/postmaster.pid" ]; then "$PGBIN/pg_ctl" -D "$PGDATA" stop -m fast >/dev/null 2>&1 || true; fi
# Do not probe the ports with connect(): under OpenShell a listen entry can outlive its
# process while rebinding still works, so a connect probe would report a phantom server.

rm -rf "$PGDATA" "$CHROME_PROFILE"
"$PGBIN/initdb" -D "$PGDATA" -U demo -A trust >/dev/null
"$PGBIN/pg_ctl" -D "$PGDATA" -o "-k /tmp -p $PGPORT -c listen_addresses=127.0.0.1" -l "$PGDATA.log" -w start >/dev/null
"$PGBIN/createdb" -h 127.0.0.1 -p "$PGPORT" -U demo demo
"$PGBIN/psql" -v ON_ERROR_STOP=1 -q -h 127.0.0.1 -p "$PGPORT" -U demo -d demo -f schema.sql
"$PGBIN/psql" -v ON_ERROR_STOP=1 -q -h 127.0.0.1 -p "$PGPORT" -U demo -d demo -f seed.sql

if [ ! -x "$VENV/bin/python" ]; then python3 -m venv "$VENV"; fi
"$VENV/bin/pip" install -q -r requirements.txt

DATABASE_URL="postgresql://demo@127.0.0.1:$PGPORT/demo" "$VENV/bin/python" app.py >"$APP_LOG" 2>&1 &
APP_PID=$!
echo "$APP_PID" > "$APP_PIDFILE"
for _ in $(seq 1 40); do
    if curl -fs -o /dev/null "$APP_URL"; then break; fi
    sleep 0.5
done
if ! curl -fs -o /dev/null "$APP_URL"; then
    echo "VERIFY FAIL: app did not answer on $APP_URL"; tail -20 "$APP_LOG" || true; exit 1
fi

CHROME=(chromium --headless=new --no-sandbox --no-zygote --disable-gpu --disable-dev-shm-usage
        --user-data-dir="$CHROME_PROFILE" --virtual-time-budget=2000)
"${CHROME[@]}" --dump-dom "$APP_URL" >"$DOM" 2>/dev/null
"${CHROME[@]}" --screenshot="$SHOT" --window-size=800,600 "$APP_URL" >/dev/null 2>&1 || true

TOTAL="$(grep -o 'id="total-cents">[0-9]*<' "$DOM" | grep -o '[0-9]*' || true)"
if [ "$TOTAL" = "600" ]; then
    echo "VERIFY PASS"
    exit 0
fi
echo "VERIFY FAIL: total was ${TOTAL:-missing}"
exit 1
