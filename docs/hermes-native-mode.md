# Hermes-native mode

**Status:** implemented (flag-gated)  
**Flag:** `HERMES_NATIVE_MODE=1`  
**Date:** 2026-06-28

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

## Follow-up (not in this slice)

- `/v1/runs` migration for structured lifecycle events
- Session ID rotation after Hermes context compression
- Slim DiscoveryTracker to thin SSE passthrough
