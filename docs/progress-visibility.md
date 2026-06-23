# Progress Visibility with DiscoveryTracker

When Hermes works on a Linear issue, it emits real-time progress updates
directly into the agent session activity timeline. This document describes
how those updates work and what to expect.

## How It Works

Instead of showing "Thinking..." or "Running read_file..." status messages,
Hermes emits **progress updates** that tell you what was actually done or
found. Each activity carries new information, written in natural language.

There are three sources of progress updates:

1. **Tool completion summaries** — After each tool call, the agent executes
   the same tool locally and extracts a meaningful summary of what was
   found (e.g. "Found 3 matches for 'class DiscoveryTracker'") rather than
   showing what tool was called.

2. **LLM finding extraction** — As the LLM generates its response, the agent
   scans for finding-like statements (sentences starting with "Found:",
   "Identified:", "Decided:", etc.), strips the prefix, and emits just the
   finding as natural text.

3. **Processing milestones** — At the start and end of each session, the
   agent emits a natural progress update ("Examining issue PLY-41",
   "Processing complete — response generated").

## What You'll See (Typical Timeline)

```
[+1s]  Examining issue PLY-41
[+6s]  Read linear_agent.py (560 lines)
[+9s]  Found 3 matches for 'class DiscoveryTracker' in linear_agent.py
[+12s] The issue asks for streaming progress updates — looking at current implementation
[+30s] Processing complete — response generated
```

Updates appear as persistent entries in the activity timeline, written in
natural prose without forced prefixes.

During long LLM calls (>3s), the stream is scanned for genuine findings.
When the LLM naturally writes something like "Found: the bug is at line 42",
the "Found:" prefix is stripped and just "the bug is at line 42" appears
as a progress update.

## LLM Finding Extraction

During streaming, the agent scans the LLM output every 3 seconds for:

1. **Keyword-prefixed findings** — Lines matching Found:, Identified:,
   Decided:, Created:, Verified:, Root cause:, etc. The keyword is
   stripped and the finding body is emitted as natural text.
2. **Natural language fallback** — Sentences starting with "the ", "this ",
   "it ", "we ", "i " followed by meaningful verbs may also be captured.
3. **In-progress sentences** — If no keyword finding exists, the first
   complete sentence from new content is emitted as an ephemeral
   in-progress indicator (replaced by the next update).

The LLM prompt includes instructions encouraging natural finding output.

## Keepalive Activities

When no new content is generated for more than 3 seconds (during pauses
in LLM streaming), the background keepalive task emits contextual messages
based on the most recent tool or analysis activity:

- "Examining issue PLY-41" (after initial examination)
- "Read linear_agent.py" (after reading a file)
- "Working on it..." (fallback when no context set)

These are ephemeral — they don't clutter the permanent timeline.

## Rate Limiting

- Progress updates (persistent): minimum 3 seconds apart
- Ephemeral in-progress updates: minimum 0.5 seconds apart
- Failed emissions are logged and silently dropped — never block work

## Design Principles

1. **Every non-keepalive activity carries new information.** If an activity
   only says what the agent is about to do, it's noise. If it says what the
   agent found or accomplished, it's signal.

2. **Natural prose, no forced prefixes.** Progress text reads like a
   coworker describing what they're doing or found — no stilted "Found:"
   or "Identified:" on every line.

3. **No emojis in activities.** Clean text-only milestones for readability.

4. **No backward disclosure.** Raw chain-of-thought, failed attempts,
   credentials, and internal prompts remain hidden.

5. **Fire-and-forget.** Activity emission never blocks or delays the actual
   work. API errors are logged and ignored.

6. **Tool-level summaries, not raw streaming.** Raw LLM text is not dumped
   into the timeline. Only extracted findings and tool-completion summaries
   appear as persistent updates.
