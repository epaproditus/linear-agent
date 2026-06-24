# Format A: Fenced `diff` block (unified diff excerpt)

**Use when:** Showing a small, focused code change (≤25 lines) after a prose summary.

The fix adds early return when the session is already complete:

```diff
--- a/linear_agent.py
+++ b/linear_agent.py
@@ -2476,6 +2476,9 @@ class TaskProcessor:
     async def _finalize_response(self, session_id, draft_text, ...):
+        if not draft_text.strip():
+            return
+
         if tracker and tracker.tool_progress:
             finalized = await self._call_llm_finalize(...)
```
