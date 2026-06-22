# Project Summary: Linear Agent (Hermes)

**Last updated:** 2026-06-21

## One-Line

A self-hosted, autonomous agent service ("Hermes") that receives Linear issues, reasons about them via a local LLM, and optionally delegates coding tasks to Claude Code — all through Linear's Agent Session API.

## Purpose

Enable the team to offload routine issue triage, analysis, and development tasks to an AI agent that lives inside our existing Linear workflow. No new UI, no context switching — just @-mention the agent and get back analysis, code, or issue updates.

## Architecture

```
Linear (user @mentions or delegates)
  │
  ▼ POST /linear/webhook
Hermes (Python/FastAPI, port 8660)
  │
  ├── HMAC-SHA256 verify → IP check → timestamp check
  ├── Acknowledge within 10s (Linear SLA)
  ├── Fetch issue via Linear GraphQL API
  ├── Route:
  │     ├── Analysis tasks → Hermes LLM API (port 8642)
  │     └── Coding tasks → Claude Code CLI (subprocess)
  ├── Emit typed activities (thought → action → response)
  └── Update issue (comment, status, delegate)

Dependencies:
  - Hermes LLM API (separate service, port 8642)
  - Claude Code (for coding tasks)
```

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Web framework | FastAPI (Python 3.13) | Async, fast, minimal boilerplate |
| ASGI server | Uvicorn | Standard for FastAPI |
| HTTP client | httpx | Async, modern, well-typed |
| Config | pydantic-settings | Type-safe env-driven config |
| Process management | systemd | Reliable auto-restart |

Total: **5 Python dependencies**. Minimal surface area.

## Current State

**Status:** Deployed and running (`linear-agent` service is active)

**Compliance:** Fully compliant with Linear's Agent Session API spec (P0/P1/P2):
- Stop signal handling (cancel in-flight tasks)
- Proper delegation detection via `delegateId`
- Auto-move to `started` workflow state
- Agent Plans (checklist visible in Linear session panel)
- Conversation history via immutable session activities
- `select` and `auth` signals for elicitation

**Security posture:**
- HMAC-SHA256 webhook verification (primary)
- IP allowlist (disabled; HMAC deemed sufficient)
- Timestamp replay protection
- Self-loop prevention
- Dedup cache (200-entry LRU)

**Codebase:** Single file, 1587 lines. All git-committed, clean working tree.

## Key Decisions

1. **Single-file monolith.** Kept as one file to avoid premature modularization. Can split as complexity grows.
2. **Separate LLM API.** The reasoning engine (Hermes API) is a separate service. This keeps the agent service thin and allows swapping LLM backends without touching the agent.
3. **Claude Code for coding tasks.** Delegating to `claude --print` rather than building code generation into the agent itself. Leverages an existing tool with broader capabilities.
4. **No external cloud dependency.** Everything runs on prem. No API keys for OpenAI/Anthropic at the agent level (the LLM service manages its own keys).

## What's Next

- [ ] Multi-session handling at scale (concurrent issue processing)
- [ ] Structured output/function calling for more reliable tool use
- [ ] Enhanced testing (integration tests against Linear sandbox)
- [ ] Modular extraction (split into service layer, clients, handlers)
- [ ] Monitoring and alerting for session failures

## Numbers

- **Lines of code:** 1,587
- **Dependencies:** 5 Python packages
- **Endpoints:** 2 (`GET /health`, `POST /linear/webhook`)
- **GraphQL operations:** 13 queries/mutations
- **Service port:** 8660 (internal)
- **LLM API port:** 8642 (internal)
