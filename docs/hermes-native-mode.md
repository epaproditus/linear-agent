# Hermes-native mode

**Status:** implemented and merged (PR #17); companion fixes in #18–#20  
**Flag:** `HERMES_NATIVE_MODE=1` (recommended for production)  
**Date:** 2026-06-30

**See also:** [linear-agent-architecture-and-learnings.md](./linear-agent-architecture-and-learnings.md) for the full redesign narrative, PLY-112 session analysis, and operational checklist.

## Goal

Make the Linear agent a **thin adapter** around Hermes — similar to how Cursor wraps one continuous agent thread — instead of orchestrating a second planning/progress system on top.

## Principles

| Hermes owns | Linear adapter owns |
|-------------|---------------------|
| Session memory & tool history | Webhook routing, dedup, stop signal |
| `todo` planning | Project `todo` → Linear Plan UI |
| Execution on agent host | Fetch issue/project/comments/guidance |
| Skills / SOUL | Map SSE → timeline activities |
| Investigation & draft | Final `response` activity + PR links |

## Session model

- **One `X-Hermes-Session-Id` per Linear `AgentSession`** (the Linear agent session UUID).
- No `:plan` or `:finalize` suffix sessions in native mode.
- Finalize (if used) is a follow-up user message in the same Hermes session.

## Planning

- **Removed:** synthetic JSON pre-flight (`_call_llm_plan`), fallback checklist, `plan.advance()` on tool progress.
- **Added:** `GET /api/todos/{hermes_session_id}` → `agentSessionUpdate(plan: …)` when Hermes calls the `todo` tool.
- No todos → no Linear plan (correct).

## Prompting

| Turn | Payload |
|------|---------|
| `created` | Issue, project, comments, guidance, user request, short Linear output rules |
| `prompted` | New user message + issue status + new comments only |

Omits: agent identity boilerplate, execution-environment block, skills catalog, synthetic plan checklist.

## Rollback

Set `HERMES_NATIVE_MODE=0` (default) to restore legacy `:plan` / fallback / `plan.advance()` behavior.

## Companion features (merged with native mode)

These work in both native and legacy modes unless noted:

| Feature | Flag / behavior | PR |
|---------|-----------------|-----|
| Prompted user message + comment deltas | Always on | #18 |
| Skip finalize on follow-ups | Always on | #19 |
| Gate issue lightweight profile | `🚧` + Human Required in description | #19 |
| Conversation watermark persistence | `~/.linear-agent/conversation_watermarks.json` | #19 |
| Blocked-by relations + deferral | `LINEAR_DEFER_ON_BLOCKERS` (default true) | #20 |

## Follow-up (not in this slice)

- `/v1/runs` migration for structured lifecycle events
- Session ID rotation after Hermes context compression
- Slim DiscoveryTracker to thin SSE passthrough
