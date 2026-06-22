# EPA-60: Verification — End-to-End Test + Crash Loop Fix Confirmation

**Status:** done
**Date:** 2026-06-22
**Agent:** CTO (fa843c08)

## Summary

Verified the linear-agent service is running stable (7h uptime) after a 44-second crash loop (restarts 605–609). All endpoints pass. Root cause was systemd restarting faster than the kernel could release the listening socket. Preventative fix applied to the service file (needs `systemctl daemon-reload` with sudo to activate).

## Crash Loop Analysis

**Timeline (2026-06-22 00:45:44 – 00:47:31):**

- 00:45:44: Restart counter 605 — uvicorn fails with `[Errno 98] address already in use`
- 00:45:50: Counter 606 — same failure
- 00:45:56: Counter 607 — same failure
- 00:46:02: Counter 608 — same failure (PID 1131248)
- 00:46:20: Counter 609 — same failure (PID 1131626)
- 00:46:28: Counter 609 retry — PID 1131844 starts successfully
- 00:47:30: Service stopped intentionally, then restarted cleanly (PID 1134675)

**Root cause:** Prior running process still held port 8660. With `RestartSec=5`, systemd retried before the kernel released the socket (TIME_WAIT defaults to 60s). Crash loop lasted 44 seconds with 5 failed restarts.

**Resolution:** Manual stop + start at 00:47:30. Current PID 1134675 has been running for 7+ hours.

## End-to-End Verification Results

| Test | Result | Evidence |
|------|--------|----------|
| Health endpoint `GET /health` | ✅ 200 | `{"status":"ok","agent":"linear-agent","backend":"claude"}` |
| Linear API auth | ✅ Authenticated | Hermes (8d529e9d-6ec0-44fc-98f0-cdd4cc4f4951@oauthapp.linear.app) |
| Webhook valid HMAC `POST /linear/webhook` | ✅ 200 | `{"status":"ignored (no mention)"}` |
| Webhook invalid HMAC | ✅ 401 | `{"detail":"Invalid signature"}` — security enforced |
| Unknown route `GET /` | ✅ 404 | Correct 404 response |
| Service uptime | ✅ 7h 24m | No crashes since fix applied |

## Preventative Fix

**File:** `linear-agent.service`

Three changes to prevent future crash loops:

1. **`ExecStartPre`** — added pre-start check that waits for port 8660 to be released before launching uvicorn
2. **`RestartSec`** — increased from 5s to 15s, giving the kernel time to release the TCP socket
3. **`TimeoutStopSec`** — increased from 10s to 15s, giving more time for graceful shutdown

**Current service runs the old config** (loaded from boot). The updated file at `/home/abe/linear-agent/linear-agent.service` needs `sudo systemctl daemon-reload && sudo systemctl restart linear-agent` to activate.

## Lab Snapshot

- Disk: 345G / 687G (53%)
- Docker containers: ~50 running (Plane, Mattermost, Firefly III, Lobe, Litellm, Fluxer, BigCapital, monitoring stack, Vaultwarden)
- Hermes ports: 8642–8651, 8660
- Key services: Hermes API, linear-agent, Plane, Mattermost, monitoring (Prometheus + Grafana + cadvisor)

## Remaining

- [ ] Run `sudo systemctl daemon-reload && sudo systemctl restart linear-agent` to apply service file hardening
- [ ] Consider adding a recurring health check (e.g., monitoring blackbox to probe `/health` every 60s)
