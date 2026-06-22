# Changelog

## 2026-06-22 — EPA-38: Full session activity reconstruction for thread context

**Option A — replaced in-memory `_last_responses` cache with Linear GraphQL session activity reconstruction.**

All changes in `linear_agent.py`.

### What changed

- **Removed** `TaskProcessor._last_responses` — fragile in-memory dict (session_id → last response) that was lost on restart and only captured one prior turn
- **Removed** 200-char truncation of prior response in prompt context
- **Added** `_format_activities_conversation()` — reconstructs full chronological conversation from all Linear session activities (prompt → thought → action → response → error), with role labels and timestamps
- **Replaced** in `_handle_analysis`: `prompted` (follow-up) events now call `get_session_activities()` via the existing `GQL_SESSION_ACTIVITIES` query (previously defined but never wired) instead of the in-memory cache
- **Preserved** issue-comments fallback when session activities are empty

### Why

The old approach was unreliable after restarts and only provided one turn of context. The new approach uses Linear's immutable activity log to reconstruct the full thread, surviving restarts and supporting multi-turn conversations.

### Verification

- Python syntax validated
- Zero dangling references to `_last_responses`
- All prompt templates preserved (prompted, @-mention, delegation)
- Service restart required: `systemctl --user restart linear-agent`

---

## 2026-06-21 — Project Summary

**Comprehensive project overview documented in `PROJECT_SUMMARY.md`.**

### Project Identity

**Name:** Hermes  
**Type:** Autonomous Linear Agent Service  
**Role:** Receives Linear issues via Agent Session API, reasons about them via a local LLM, and delegates coding tasks to Claude Code.

### Architecture (high-level)

```
Linear (user @mentions or delegates)
  → POST /linear/webhook
  → Hermes (FastAPI, port 8660)
    → HMAC verify → Acknowledge (10s SLA)
    → Fetch issue via Linear GraphQL
    → Route: Analysis → LLM API (port 8642)
             Coding → Claude Code CLI
    → Emit activities (thought → action → response)
    → Update issue
```

### Key Facts

| Metric | Value |
|--------|-------|
| Codebase | 1,587 lines (single-file) |
| Dependencies | 5 Python packages |
| Endpoints | 2 (health, webhook) |
| GraphQL ops | 13 queries/mutations |
| Service port | 8660 (internal) |
| LLM API port | 8642 (internal) |
| Compliance | Linear Agent Session API P0/P1/P2 |
| Security | HMAC-SHA256, timestamp replay protection, self-loop prevention, dedup cache |

### Current State

Deployed and running via systemd. Fully compliant with Linear's Agent Session API spec. Single-file monolith with 5 Python deps. No external cloud dependencies.

### Next Priorities

1. Multi-session concurrency at scale
2. Structured output / function calling
3. Integration tests against Linear sandbox
4. Modular extraction from single file
5. Monitoring and alerting

---



## 2026-06-21 — GraphQL Type Fix

**`GQL_TEAM_STATES` variable type**
- Changed `$teamId: String!` → `$teamId: ID!`
- Linear's schema requires `ID!` in filter positions (`{ id: { eq: $teamId } }`); `String!` caused `GRAPHQL_VALIDATION_FAILED` at runtime
- Other queries using `String!` for direct mutation/query args are unaffected

---

## 2026-06-21 — Linear API Compliance Fixes

All changes in `linear_agent.py`. Based on audit against Linear's Agent Session API docs:
`GETTING_STARTED.md`, `DEVELOPING_AGENT_INTERACTION.md`, `INTERACTION_BEST_PRACTICES.md`, `SIGNALS.md`.

### P0 — Broken behavior fixed

**Stop signal handling**
- `_active_runs` changed from `set[str]` to `dict[str, asyncio.Task]` so tasks can be cancelled by session ID
- `_handle_agent_session` now checks `agentActivity.signal == "stop"` before spawning any background task
- If a task is running, it is cancelled via `task.cancel()`; a `response` activity confirms the halt to the user
- `_run_session` catches `asyncio.CancelledError` and re-raises cleanly so asyncio task cleanup works correctly

**Delegation detection (`delegateId`)**
- `_handle_issue_update` now checks both `delegateId` and `assigneeId` against the agent's viewer ID
- Linear sets `delegateId` (not `assigneeId`) when a user delegates an issue to an agent
- `process()` now calls `update_issue(delegateId=viewer_id)` at session start if no delegate is set on the issue
- Added `delegate { id name email }` field to `GQL_ISSUE_BY_ID`

### P1 — Spec non-compliance fixed

**Auto-move issue to `started` on any delegation**
- `process()` now moves the issue to the first `started` workflow state if the current state type is not `started`, `completed`, or `canceled`
- This runs for all session types (analysis + coding), not just coding tasks
- Removed the duplicate status-move block that was only inside `_handle_dev_task`

**Conversation history via session activities**
- Added `GQL_SESSION_ACTIVITIES` query fetching `agentSession.activities` with inline fragments for all content types (`__typename` included)
- Added `LinearClient.get_session_activities(session_id)` method
- `_handle_analysis` now uses activities (not issue comments) for `prompted` (multi-turn) events — activities are immutable snapshots; comments are editable and unreliable per Linear docs
- For initial `created` sessions, issue comments are still used as before

### P2 — Missing features added

**Agent Plans**
- Added `LinearClient.update_plan(session_id, steps)` — wraps `agentSessionUpdate` with `plan` field
- `process()` emits a 2-step plan at session start (`inProgress`) and marks both steps `completed` after work finishes
- Plan is visible in the Linear session panel as a checklist

**`select` and `auth` signals on elicitation activities**
- `LinearClient.create_activity` now accepts `signal` and `signal_metadata` keyword args, passed through to `agentActivityCreate`
- `LinearClient.send_elicitation_select(session_id, body, options)` — presents a list of options to the user via the `select` signal
- `LinearClient.send_elicitation_auth(session_id, body, url, *, user_id, provider_name)` — triggers Linear's account-linking UI via the `auth` signal
