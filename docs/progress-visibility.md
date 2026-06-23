# Progress Visibility with DiscoveryTracker

When Hermes works on a Linear issue, it emits real-time progress updates
directly into the agent session activity timeline. This document describes
how those updates work and what to expect.

## How It Works

Instead of showing "Thinking..." or "Running read_file..." status messages,
Hermes emits **discovery activities** that tell you what was actually found
or accomplished. Each activity carries new information.

## Discovery Kinds

| Activity | Meaning | Example |
|----------|---------|---------|
| Found | A piece of information located during investigation | "Found: streaming code spans lines 1523-1665 in linear_agent.py" |
| Identified | A gap, pattern, or relationship recognized | "Identified: three gaps in current activity emission model" |
| Decided | A choice made between alternatives | "Decided: result-oriented model over phase badges" |
| Created | An artifact produced | "Created: implementation plan at PLY-40.md" |
| Verified | A validation completed | "Verified: all existing tests pass after changes" |

## What You'll See (Typical Timeline)

```
[+1s]  Found: Processing PLY-41 — Implement streaming progress updates
[+5s]  Found: streaming infrastructure at lines 1738-1895 in linear_agent.py
[+10s] Identified: three gaps — no result-oriented content, raw tool JSON, generic keepalive
[+15s] Decided: emit findings instead of announcements; result-oriented model
[+20s] Still: creating DiscoveryTracker class...
[+30s] Created: DiscoveryTracker class with rate-limited emission
[+35s] Responding with implementation summary...
[+40s] Done: PLY-41 — DiscoveryTracker implemented (9 files changed, 1 new)
```

## Keepalive Activities

When no new discovery is ready (during long LLM calls), Hermes emits
contextual keepalive messages that reference what's currently being
investigated:

- "Still: creating DiscoveryTracker class..."
- "Still: integrating into _handle_analysis..."
- "Still working on it..." (fallback)

These are ephemeral — they don't clutter the permanent timeline.

## Rate Limiting

- Milestone discoveries (Found, Identified, etc.): minimum 3 seconds apart
- Any activity (including in-progress): minimum 1.5 seconds apart
- Failed emissions are logged and silently dropped — never block work

## Design Principles

1. **Every non-keepalive activity carries new information.** If an activity
   only says what the agent is about to do, it's noise. If it says what the
   agent found or accomplished, it's signal.

2. **No emojis in activities.** Clean text-only milestones for readability.

3. **No backward disclosure.** Raw chain-of-thought, failed attempts,
   credentials, and internal prompts remain hidden.

4. **Fire-and-forget.** Activity emission never blocks or delays the actual
   work. API errors are logged and ignored.
