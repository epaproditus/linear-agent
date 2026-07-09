#!/bin/bash
set -e

PORT=8660
APP="/home/abe/linear-agent/.venv/bin/uvicorn"
APP_ARGS="linear_agent:app --host 0.0.0.0 --port $PORT --log-level info"
HEALTH_URL="http://127.0.0.1:$PORT/health"

# Kill any orphan process holding the port (handles stale processes
# that survived a wrapper restart but keep the port bound).
# Using --kill with TERM first, then KILL — clean and guaranteed.
fuser -k -TERM "$PORT/tcp" 2>/dev/null || true

# Wait for port to be released (handles TIME_WAIT after restart)
for i in $(seq 1 30); do
    if ! ss -tlnp sport = :$PORT 2>/dev/null | grep -q :$PORT; then
        break
    fi
    sleep 2
done

cd /home/abe/linear-agent

$APP $APP_ARGS &
PID=$!

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
