# Format E: Summary + diff excerpt hybrid (recommended)

**Use when:** Any non-trivial code change in a Hermes `response` activity.

## Finding

Empty investigation drafts were still sent through the phase-2 rewrite, wasting tokens and occasionally producing hollow replies.

## Change

Added an early return in `_finalize_response` when `draft_text` is blank.

**`linear_agent.py`** — guard before phase-2 LLM call:

```diff
@@ -2476,6 +2476,9 @@ class TaskProcessor:
     async def _finalize_response(self, session_id, draft_text, ...):
+        if not draft_text.strip():
+            return
+
         if tracker and tracker.tool_progress:
             finalized = await self._call_llm_finalize(...)
```

## Why this format

- Prose summary is scannable in Linear’s comment thread.
- The `diff` block highlights additions (green) and deletions (red) when the renderer supports the `diff` language tag.
- Keeps each excerpt short; link to a PR for full diffs.
