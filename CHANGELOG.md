# Changelog

## [Unreleased]

### Added
- Architecture documentation: `docs/linear-agent-architecture-and-learnings.md` (full redesign
  narrative, PLY-112 session analysis, deploy checklist) and paste-ready Linear status
  comment at `docs/linear-activity-status-2026-06-30.md`.
- PR #20: Linear blocked-by/blocks relations in GraphQL fetch, prompt injection, and
  deferral on `created` turns when open blockers exist (`LINEAR_DEFER_ON_BLOCKERS`).
- PR #19: Skip phase-2 finalize on `prompted` turns; human gate issue lightweight profile;
  persistent conversation watermarks at `~/.linear-agent/conversation_watermarks.json`.
- PR #18: Correct `user_request` on prompted turns (never issue description fallback);
  comment delta injection via per-session watermarks; threaded comment dedupe.
- PR #17: Hermes-native mode (`HERMES_NATIVE_MODE=1`) — one Hermes session per Linear
  agent session, todo→plan projection, minimal prompts.
- EPA-58: Activity timeout guard — background keepalive task emits
  "Still thinking..." ephemeral thoughts every 45s during long LLM
  processing, preventing Linear from marking sessions as unresponsive.
  (`linear_agent.py:_keep_session_alive`, `linear_agent.py:process`)
- EPA-58: Immediate "Thinking..." ephemeral thought before launching
  LLM call for instant user feedback. (`linear_agent.py:process`)
- EPA-57: Success logging to `_call_llm` — log response length and elapsed
  time on every successful 200 response. Mute episodes are now traceable:
  no success log = no response sent. (`linear_agent.py:_call_llm`)
- EPA-57: Retry logic in `_call_llm` — 1 retry with 5s backoff on timeout
  or 5xx responses. Prevents transient Hermes API failures from dropping
  sessions. (`linear_agent.py:_call_llm`)
- EPA-57: `send_response`/`send_error` outcome logging — every response
  and error delivery is now logged with success status and payload length.
  Enables response delivery traceability through the full pipeline.
  (`linear_agent.py:send_response`, `linear_agent.py:send_error`)
- EPA-41: Injected LINEAR_API_KEY into all three Hermes prompt variants in
  `_handle_analysis()` so the agent can act on Linear directly.
  - prompted (follow-up turn)
  - @-mention (explicit user message)
  - delegation (issue assigned as task)
  Each prompt now contains: `Your LINEAR_API_KEY for GraphQL API calls to
  api.linear.app: {settings.linear_api_key}`
