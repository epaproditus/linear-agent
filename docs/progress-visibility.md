# Progress Visibility with DiscoveryTracker

When Hermes works on a Linear issue, it emits real-time progress updates
directly into the agent session activity timeline. This document describes
how those updates work and what to expect.

## How It Works

Instead of showing "Thinking..." or "Running read_file..." status messages,
Hermes emits **discovery activities** that tell you what was actually found
or accomplished. Each activity carries new information.

There are two sources of discoveries:

1. **LLM text stream extraction** — As the LLM generates its response, the
   Linear agent scans the stream for finding-like statements (sentences
   starting with "Found:", "Identified:", "Decided:", "Created:", etc.)
   and emits them as discoveries. When no explicit finding is found, the
   first meaningful sentence from new content is emitted as an in-progress
   update.

2. **Investigation milestones** — Before and after the LLM call, the agent
   emits milestone activities (initial discovery, final response summary).

Note: Unlike the original plan, tool calls are NOT intercepted at the
Linear agent level. The Hermes API server executes all tools internally
and returns only text. All discoveries come from the text stream.

## Discovery Kinds

| Kind | Meaning | Example |
|------|---------|---------|
| Found | A piece of information located | "Found: streaming code at line 1523" |
| Identified | A gap, pattern, or relationship | "Identified three gaps in activity emission" |
| Decided | A choice between alternatives | "Decided to use result-oriented model" |
| Created | An artifact produced | "Created implementation plan" |
| Verified | A validation completed | "Verified all tests pass after changes" |

## What You'll See (Typical Timeline)

```
[+1s]  Found: Processing PLY-41 — Implement streaming progress updates
[+3s]  Still: analyzing the issue and planning next steps
[+6s]  (in-progress) Let me start by exploring the codebase to understand...
[+15s] (in-progress) Found the streaming infrastructure at lines 1523-1665
[+30s] Created: Response generated for PLY-41
```

During long LLM calls (>3s), new text content is emitted as in-progress
activities every ~3 seconds. If the LLM follows the finding-prefix format
(Found:, Identified:, etc.), these appear as real discovery milestones.

## LLM Finding Extraction

During streaming, the Linear agent scans the LLM output every 3 seconds for:

1. **Keyword-prefixed** — Lines matching Found:, Identified:, Decided:,
   Created:, Verified:, Root cause:, The issue is:, etc.
2. **Natural language fallback** — Sentences starting with "the ", "this ",
   "it ", "we ", "i " followed by meaningful verbs.
3. **Content sentence extraction** — If no finding keywords are found, the
   first complete sentence from new stream content is emitted as an
   in-progress indicator.

The LLM prompt includes instructions encouraging natural finding output.

## Keepalive Activities

When no new content is generated for more than 3 seconds (during pauses
in LLM streaming), the background keepalive task emits contextual messages:

- "Still: analyzing the issue and planning next steps" (initial context)
- "Still working on it..." (fallback when no context set)

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

5. **Text-stream only.** Tool calls happen inside the Hermes API server
   and are not visible to the Linear agent. All discoveries derive from
   the text the LLM generates.
