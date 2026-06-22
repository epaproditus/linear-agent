#!/bin/bash
# Updates the systemd unit, reloads, and restarts linear-agent.
# Run with: bash ~/linear-agent/bin/update-service.sh

set -euo pipefail

echo "=== Installing updated systemd unit ==="
sudo cp /home/abe/linear-agent/linear-agent.service /etc/systemd/system/linear-agent.service

echo "=== Reloading systemd ==="
sudo systemctl daemon-reload

echo "=== Restarting linear-agent ==="
sudo systemctl restart linear-agent

echo "=== Waiting for service to start ==="
sleep 3

echo "=== Status ==="
sudo systemctl status linear-agent --no-pager -l
