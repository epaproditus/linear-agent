# Format B: Fenced `patch` block

**Use when:** Testing alternate language tag; behavior is usually identical to `diff` in GitHub-style renderers.

```patch
diff --git a/linear_agent.py b/linear_agent.py
index 1a2b3c4..5d6e7f8 100644
--- a/linear_agent.py
+++ b/linear_agent.py
@@ -89,7 +89,8 @@ HERMES_REPLY_STYLE = """
 - Open with the finding, answer, or decision — not setup or process narration.
-- Reference code with citation fences: ```startLine:endLine:filepath (Cursor/Linear format).
+- Reference existing code with citation fences: ```startLine:endLine:filepath.
+- Show code changes with short ```diff excerpts (≤25 lines per file).
 - For complex logic or architecture, include a ```mermaid diagram when it clarifies the flow.
```
