#!/bin/bash
# Installs/updates the user-systemd unit, reloads, and restarts linear-agent.
# No sudo needed — runs entirely in user context.
# Run with: bash ~/linear-agent/bin/update-service.sh

set -euo pipefail

SERVICE_SRC="/home/abe/linear-agent/linear-agent-user.service"
SERVICE_DST="$HOME/.config/systemd/user/linear-agent.service"

mkdir -p "$(dirname "$SERVICE_DST")"

echo "=== Installing updated systemd unit ==="
cp "$SERVICE_SRC" "$SERVICE_DST"

echo "=== Reloading systemd (user) ==="
systemctl --user daemon-reload

echo "=== Restarting linear-agent ==="
systemctl --user restart linear-agent

echo "=== Waiting for service to start ==="
sleep 5

echo "=== Status ==="
systemctl --user status linear-agent --no-pager -l
