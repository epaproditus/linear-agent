# Format C: Plain fenced code block (no language tag)

**Use when:** Fallback only. No +/- coloring; harder to scan than `diff`.

Changed the finalize guard in `linear_agent.py`:

```
     async def _finalize_response(self, session_id, draft_text, ...):
+        if not draft_text.strip():
+            return
+
         if tracker and tracker.tool_progress:
             finalized = await self._call_llm_finalize(...)
```
