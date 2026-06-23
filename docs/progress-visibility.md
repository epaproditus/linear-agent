# Progress Visibility with DiscoveryTracker

When Hermes works on a Linear issue, it emits real-time progress updates
directly into the agent session activity timeline. This document describes
how those updates work and what to expect.

## How It Works

Instead of showing "Thinking..." or "Running read_file..." status messages,
Hermes emits **discovery activities** that tell you what was actually found
or accomplished. Each activity carries new information.

There are two sources of discoveries:

1. **Tool result extraction** — When Hermes calls a tool (read_file, search_files),
   the linear agent executes the same tool locally on the same machine and
   extracts a discovery from the result.
2. **LLM finding extraction** — As the LLM generates its response, the linear
   agent scans the stream for finding-like statements (sentences starting with
   "Found:", "Identified:", "The issue is:", etc.) and emits them as discoveries.

## Discovery Kinds

| Kind | Meaning | Example |
|------|---------|---------|
| Found | A piece of information located | "Read linear_agent.py (650 lines): class DiscoveryTracker..." |
| Identified | A gap, pattern, or relationship | "Identified three gaps in activity emission" |
| Decided | A choice between alternatives | "Decided to use result-oriented model" |
| Created | An artifact produced | "Response generated for PLY-41" |
| Verified | A validation completed | "Verified all tests pass after changes" |

## What You'll See (Typical Timeline)

```
[+1s]  Found: Processing PLY-41 — Implement streaming progress updates
[+3s]  Found: Read linear_agent.py: class DiscoveryTracker at line 1064
[+6s]  Found: Searching for 'DiscoveryTracker' in linear_agent.py
[+10s] Still: exploring the codebase...
[+15s] Found: The implementation is missing tool result extraction
[+20s] Response generated for PLY-41
```

## Tool Result Extraction

After each tool call, the linear agent executes the same tool locally in
parallel and extracts a discovery statement from the result:

- **read_file** → "Read {path} ({lines} lines): {first 80 chars}"
- **search_files** → "Found {N} matches for '{pattern}' in {path}"
- **web_extract** → "Fetched {url} ({N} chars)"
- **execute_code** → "Code executed: {output preview}"

If local execution fails or the tool isn't available locally
(e.g., web_search without API key), a lightweight discovery is
generated from the tool name and arguments alone.

## LLM Finding Extraction

During streaming, the linear agent scans the LLM output every 5 seconds for
finding statements:

1. **Keyword-prefixed** — Lines starting with Found:, Identified:, Decided:,
   Created:, Verified:, Root cause:, The issue is:, etc.
2. **Natural language fallback** — Sentences starting with "the ", "this ",
   "it ", "we ", "i " followed by meaningful verbs (is, was, has, contains,
   shows, reveals, indicates, etc.)

The prompt includes instructions encouraging the LLM to output findings
naturally as it works.

## Keepalive Activities

When no new discovery is ready for more than 5 seconds (during long LLM
calls), Hermes emits contextual keepalive messages:

- "Still: exploring the codebase..."
- "Still: analyzing the streaming loop..."
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
