# Format F: Truncated large diff with ellipsis marker

**Use when:** Change spans many files or hunks — show only the entry point, not the full patch.

Refactored progress formatting across 4 files. Core behavior change in `format_hermes_tool_progress`:

```diff
@@ -1515,10 +1515,14 @@ def _normalize_progress_markdown(text: str) -> str:
 def format_hermes_tool_progress(payload: dict) -> str | None:
     tool = payload.get("tool") or payload.get("name") or ""
     status = payload.get("status") or ""
+    if status not in ("completed", "done", "success"):
+        return None
     ...
```

*(3 additional files changed — see PR for full diff.)*
