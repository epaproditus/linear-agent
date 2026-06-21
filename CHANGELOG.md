# Changelog

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
