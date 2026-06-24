#!/usr/bin/env python3
"""Generate terminal rendering notes for PLY-43 diff format fixtures."""

from __future__ import annotations

from pathlib import Path

from pygments import highlight
from pygments.formatters import Terminal256Formatter
from pygments.lexers import DiffLexer, TextLexer, get_lexer_by_name

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "docs" / "fixtures" / "ply-43-diff-formats"

SAMPLES = {
    "diff": """--- a/linear_agent.py
+++ b/linear_agent.py
@@ -2476,6 +2476,9 @@
     async def _finalize_response(self, session_id, draft_text, ...):
+        if not draft_text.strip():
+            return
+
         if tracker and tracker.tool_progress:""",
    "patch": """diff --git a/linear_agent.py b/linear_agent.py
--- a/linear_agent.py
+++ b/linear_agent.py
@@ -89,7 +89,8 @@
-- old line
++ new line""",
    "plain": """     async def _finalize_response(...):
+        if not draft_text.strip():
+            return""",
}


def render_sample(name: str, code: str, lexer_name: str | None = None) -> str:
    if lexer_name == "diff":
        lexer = DiffLexer()
    elif lexer_name == "patch":
        try:
            lexer = get_lexer_by_name("patch")
        except Exception:
            lexer = DiffLexer()
    else:
        lexer = TextLexer()
    return highlight(code, lexer, Terminal256Formatter(style="monokai"))


def main() -> None:
    print("PLY-43 local rendering notes (Pygments)\n")
    print("=" * 60)
    for label, lexer in [("diff", "diff"), ("patch", "patch"), ("plain", None)]:
        print(f"\n## {label.upper()} lexer\n")
        print(render_sample(label, SAMPLES[label], lexer))
    print("\n" + "=" * 60)
    print(f"\nFixture markdown files: {FIXTURES}")
    md_files = sorted(FIXTURES.glob("*.md"))
    print(f"Count: {len(md_files)}")
    for p in md_files:
        print(f"  - {p.name} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
