# Format D: Cursor-style code citation (existing code reference)

**Use when:** Pointing at unchanged or post-change code in the repo — not for showing +/- hunks.

The finalize path short-circuits on empty drafts:

```2476:2482:linear_agent.py
    async def _finalize_response(self, session_id, draft_text, ...):
        if not draft_text.strip():
            return

        if tracker and tracker.tool_progress:
            finalized = await self._call_llm_finalize(...)
```
