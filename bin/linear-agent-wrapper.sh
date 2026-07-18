#!/bin/bash
set -e

PORT=8660
APP="/home/abe/linear-agent/.venv/bin/uvicorn"
APP_ARGS="linear_agent:app --host 0.0.0.0 --port $PORT --log-level info"
HEALTH_URL="http://127.0.0.1:$PORT/health"

# Kill any stale uvicorn processes holding our port (orphaned from prior restarts)
fuser -k "${PORT}/tcp" 2>/dev/null || true

# Then wait briefly for port to fully release (handles TIME_WAIT)
for i in $(seq 1 10); do
    if ! ss -tlnp "sport = :$PORT" 2>/dev/null | grep -q :"$PORT"; then
        break
    fi
    sleep 1
done

cd /home/abe/linear-agent

$APP $APP_ARGS &
UVICORN_PID=$!

# Ensure uvicorn is cleaned up if the wrapper is killed by systemd
cleanup() {
    if [ -n "$UVICORN_PID" ] && kill -0 "$UVICORN_PID" 2>/dev/null; then
        kill "$UVICORN_PID" 2>/dev/null || true
        wait "$UVICORN_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

for i in $(seq 1 15); do
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        systemd-notify --ready
        break
    fi
    sleep 1
done

while true; do
    sleep 15
    if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
        systemd-notify WATCHDOG=1
    else
        systemd-notify ERRNO=5
        exit 1
    fi
done
