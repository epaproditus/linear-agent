# AGENTS.md

## Cursor Cloud specific instructions

This repo is a single-file Python/FastAPI service: the **Linear AI Agent** (`linear_agent.py`),
a webhook receiver on **port 8660** that verifies Linear webhooks (HMAC-SHA256), routes events,
calls the Linear GraphQL API + an OpenAI-compatible "Hermes" LLM API, and optionally delegates
coding tasks to `claude`/`codex` CLIs. There is **no database**.

Dependencies are installed into a virtualenv at `.venv` by the startup update script
(`python3 -m venv .venv` + `pip install -r requirements.txt`). Use `.venv/bin/...` to run tools.

### Run (dev)
- The module-level `assert settings.configured` (in `linear_agent.py`) means the app **will not import/start**
  unless `LINEAR_API_KEY` and `LINEAR_WEBHOOK_SECRET` are set. A `.env` (gitignored) with dummy values is
  sufficient for local dev; copy from `.env.example` if missing.
- For local testing set `LINEAR_ENFORCE_IP_ALLOWLIST=false` (otherwise loopback requests get a 403, since only
  known Linear IP ranges are allowed) and `CODING_AGENT=none` (so it won't shell out to `claude`/`codex`).
- Start the dev server: `.venv/bin/uvicorn linear_agent:app --host 0.0.0.0 --port 8660 --reload`
- On startup the app does a Linear auth check; with dummy credentials this logs a `401 ... API auth check failed
  (will retry)` warning and then completes startup normally — this is expected, not a crash.

### Test / lint
- There is **no test suite and no configured linter** (no pytest/ruff/flake8 config). The closest lint-equivalent
  is `.venv/bin/python -m py_compile linear_agent.py`.
- Smoke test without external services: `GET /health` returns `{"status":"ok",...}`; a `POST /linear/webhook`
  with a wrong `Linear-Signature` returns 401; a request whose `Linear-Signature` is the hex HMAC-SHA256 of the
  raw body (key = `LINEAR_WEBHOOK_SECRET`) passes verification and is routed by `handle_event`.

### Notes
- `setup.sh` and `bin/*.sh` install/run a **systemd** service and contain hardcoded paths (e.g. `/home/abe/...`);
  they are deployment helpers and are not needed for local dev — run `uvicorn` directly instead.
- Full end-to-end behavior (posting back to Linear, LLM reasoning) requires real `LINEAR_API_KEY`,
  `LINEAR_WEBHOOK_SECRET`, and `HERMES_API_KEY` plus a reachable Hermes API at `HERMES_API_URL`
  (default `http://127.0.0.1:8642/v1`) — not available in this environment by default.
