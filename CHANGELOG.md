# Changelog

## [Unreleased]

### Added
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
