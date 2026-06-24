# Progress Visibility with DiscoveryTracker

When Hermes works on a Linear issue, it emits real-time progress updates
directly into the agent session activity timeline. This document describes
how those updates work and what to expect.

## How It Works

Hermes runs as a full agent via the Hermes API server (port 8642). Tools
(terminal, files, web search, code execution, etc.) execute **server-side**
inside Hermes — linear-agent does not re-run tools locally.

Progress comes from Hermes' custom SSE event `hermes.tool.progress`, which
is separate from the final assistant response text. That separation means
timeline updates show what Hermes is doing without duplicating the answer.

There are three sources of progress updates:

1. **Hermes tool progress** — As Hermes runs tools during its agent loop,
   each `hermes.tool.progress` event is converted to natural text (e.g.
   "git remote -v", "Searching web for 'PLY-41 discovery tracker'") and
   emitted to Linear via DiscoveryTracker.

2. **Content-drought keepalive** — During long thinking phases with no
   tool or content tokens, contextual keepalive text fires every ~5 seconds
   based on the most recent tool progress.

3. **Session milestones** — At session start: "Examining issue PLY-41".

The final answer is emitted once via `send_response()` — never streamed
into the timeline.

## What You'll See (Typical Timeline)

```
[+1s]  Examining issue PLY-41
[+6s]  git remote -v
[+9s]  grep -r DiscoveryTracker linear_agent.py
[+12s] Reading linear_agent.py
[+30s] (final response)
```

All text is natural prose. No labels, no prefixes, no emojis.

## Keepalive Activities

When no new tool progress arrives for more than 15 seconds, the background
keepalive task emits contextual messages based on the most recent activity:

- "Examining issue PLY-41" (after initial examination)
- "git remote -v" (after a terminal command)
- "Working on it..." (fallback when no context set)

No "Still:" prefix. These are ephemeral.

## DiscoveryTracker Methods

All methods produce natural text with no kind labels:

| Method | Type | Use |
|--------|------|-----|
| `in_progress(description)` | Ephemeral | Ongoing work, replaced by next update |
| `progress(detail)` | Persistent | Tool progress, intermediate findings |
| `found(detail)` | Persistent | Alias for progress() |
| `identified(detail)` | Persistent | Alias for progress() |
| `decided(detail)` | Persistent | Alias for progress() |
| `created(detail)` | Persistent | Alias for progress() |
| `verified(detail)` | Persistent | Alias for progress() |

## Rate Limiting

- Progress updates (persistent): minimum 1.5 seconds apart
- Ephemeral in-progress updates: minimum 0.5 seconds apart
- Failed emissions are logged and silently dropped — never block work

## Design Principles

1. **Tool progress, not response text.** Hermes tool events surface on the
   timeline; the final LLM answer appears only once as the response.

2. **Natural prose, no forced prefixes.** Progress text reads like a
   coworker describing what they're doing.

3. **No emojis in activities.** Hermes may send emoji in progress payloads;
   linear-agent strips them and emits clean text only.

4. **No backward disclosure.** Raw chain-of-thought, failed attempts,
   credentials, and internal prompts remain hidden.

5. **Non-blocking emission.** Activity POSTs run via ProgressQueueWorker
   so the SSE reader never stalls on Linear API latency.
