# Progress Visibility with DiscoveryTracker

When Hermes works on a Linear issue, it emits real-time progress updates
directly into the agent session activity timeline. This document describes
how those updates work and what to expect.

## How It Works

Instead of showing "Thinking..." or "Running read_file..." status messages,
Hermes emits **progress updates** that tell you what was actually done or
found. Each activity carries new information, written in natural language
with no prefixes, labels, or emojis.

There are three sources of progress updates:

1. **Tool completion summaries** — After each tool call, the agent executes
   the same tool locally and extracts a meaningful summary (e.g. "Read
   linear_agent.py (560 lines): class DiscoveryTracker...") rather than
   showing what tool was called.

2. **LLM streaming sentences** — As the LLM generates its response, the
   first complete sentence of new content is emitted every 1+ seconds as
   an ephemeral in-progress update. No keyword scanning, no "Found:" or
   "Identified:" prefixes — just whatever the LLM is writing.

3. **Processing milestones** — At the start and end of each session, the
   agent emits natural text ("Examining issue PLY-41", "Processing complete
   — response generated").

## What You'll See (Typical Timeline)

```
[+1s]  Examining issue PLY-41
[+6s]  Read linear_agent.py (560 lines)
[+9s]  Found 3 matches for 'class DiscoveryTracker' in linear_agent.py
[+12s] The issue asks for streaming progress updates — looking at current implementation
[+30s] Processing complete — response generated
```

All text is natural prose. No labels, no prefixes, no emojis.

## LLM Streaming Progress

During streaming LLM calls, every 1+ seconds the agent:

1. Checks for new content since the last check (using `_last_checked_len`)
2. Extracts the first complete sentence from the new content
3. Emits it as an ephemeral in-progress update via `tracker.in_progress()`

If no complete sentence is found (mid-sentence fragment), the content is
saved as keepalive context so the background timer says something relevant.

No keyword scanning, no pattern matching, no prefix stripping. The output
reads like cursor's progress — whatever the LLM is actually writing, shown
as it's written.

## Keepalive Activities

When no new content is generated for more than 15 seconds (during pauses
in LLM streaming or between tool calls), the background keepalive task
emits contextual messages based on the most recent activity:

- "Examining issue PLY-41" (after initial examination)
- "Read linear_agent.py" (after reading a file)
- "Working on it..." (fallback when no context set)

No "Still:" prefix. The keepalive returns the context directly, with
first-letter capitalization for natural reading.

These are ephemeral — they don't clutter the permanent timeline.

## DiscoveryTracker Methods

All methods produce natural text with no kind labels:

| Method | Type | Use |
|--------|------|-----|
| `in_progress(description)` | Ephemeral | Ongoing work, replaced by next update |
| `progress(detail)` | Persistent | Tool results, intermediate findings |
| `found(detail)` | Persistent | Alias for progress() |
| `identified(detail)` | Persistent | Alias for progress() |
| `decided(detail)` | Persistent | Alias for progress() |
| `created(detail)` | Persistent | Alias for progress() |
| `verified(detail)` | Persistent | Alias for progress() |

All persistent methods emit the same way: `label=""`, `body=detail` to
Linear's API. The method names exist for code readability only — the
output has no category prefix.

## Rate Limiting

- Progress updates (persistent): minimum 1.5 seconds apart
- Ephemeral in-progress updates: minimum 0.5 seconds apart
- Failed emissions are logged and silently dropped — never block work

## Design Principles

1. **Every non-keepalive activity carries new information.** If an activity
   only says what the agent is about to do, it's noise. If it says what the
   agent found or accomplished, it's signal.

2. **Natural prose, no forced prefixes.** Progress text reads like a
   coworker describing what they found — no "Found:", "Identified:", or any
   category badge on the text.

3. **No emojis in activities.** Clean text-only milestones.

4. **No backward disclosure.** Raw chain-of-thought, failed attempts,
   credentials, and internal prompts remain hidden.

5. **Fire-and-forget.** Activity emission never blocks or delays the actual
   work. API errors are logged and ignored.

6. **Tool-level summaries, not raw streaming.** Raw LLM text is not dumped
   into the timeline. Only extracted sentences and tool-completion summaries
   appear as updates.
