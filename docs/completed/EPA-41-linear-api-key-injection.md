# EPA-41: Inject LINEAR_API_KEY into Hermes API prompts

**Status:** done  
**Date:** 2026-06-22  
**Agent:** CTO (fa843c08)

## Summary

Injected `LINEAR_API_KEY` into all three prompt templates in `_handle_analysis()` so Hermes agents can make direct Linear GraphQL API calls without manual instruction.

## Changes

**File:** `linear_agent.py`

Three prompt branches updated in `_handle_analysis()`:

| Branch | Line | Injection |
|--------|------|-----------|
| `prompted` (follow-up turn) | 1120 | `Your LINEAR_API_KEY for GraphQL API calls to api.linear.app: {settings.linear_api_key}` |
| `@-mention` (explicit message) | 1135 | Same |
| Delegated issue | 1152 | Same |

## Verification

- `settings.linear_api_key` is a declared `Settings` field (line 77)
- `settings_valid` validates it is non-empty (line 116)
- Used to construct `LinearClient` (line 1556)
- Python syntax check: OK

## How Hermes uses this

The key is embedded in the prompt as a readable value. Hermes can:
1. Read it from its own prompt context
2. Use it as `Bearer` token in `Authorization` header for `https://api.linear.app/graphql`
3. Create projects, list teams, update issues, etc. directly

No `.env` path guessing needed — the value is right there in the prompt.
