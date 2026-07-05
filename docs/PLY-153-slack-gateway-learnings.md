# PLY-153 — Linear agent per-turn feeding vs Slack gateway

**Status:** implemented in native mode (2026-07-05)  
**Fixture:** [sample_slack_conversation.json](./fixtures/sample_slack_conversation.json)

---

## Question

What does the Slack gateway feed Hermes **each turn**, and how does that differ from what linear-agent was feeding?

---

## Slack gateway (from `sample_slack_conversation.json`)

Each user turn is a **single user message** shaped like:

```
[Replying to: "New Assistant Thread"]

[Thread context — prior messages in this thread (not yet in conversation history). …]
Abraham: <prior human message>
Abraham: <another prior human message>
[End of thread context]

<new user message only>
```

| Turn | What gets injected | What Hermes session already holds |
|------|-------------------|-----------------------------------|
| **1** | Thread parent + new message (~660 chars) | Nothing yet |
| **2+** | Growing thread-context block + new message | All prior assistant text, tool calls, tool results |

**Not re-fed on follow-ups:** assistant replies, tool outputs, skills catalog, system prompts, issue metadata.

The gateway only prepends **human thread messages that are not yet in the Hermes session** plus the **current message**.

---

## Linear agent (before PLY-153)

| Turn | What linear-agent fed |
|------|----------------------|
| **created** | Issue card (description, project, siblings, guidance, relations) **and** `Full conversation (all comments)` **and** `User request:` wrapper — often duplicating the @mention |
| **prompted** | `Follow-up on …`, status, `User message:`, `New comments since your last turn:` (different format), relations, blockers, `LINEAR_OUTPUT_RULES` — re-injecting metadata Hermes already had from turn 1 |

This diverged from the Slack gateway thin per-turn model and grew prompts on every follow-up.

---

## Linear agent (after PLY-153, native mode)

Mirrors Slack gateway feeding via `build_thread_context_block()` and `build_native_turn_message()`:

| Turn | Payload |
|------|---------|
| **created** | One-time issue card + Slack-style thread context (prior human comments, excluding current) + raw user message + first-turn hints/rules |
| **prompted** | `[Replying on Linear issue …]` + thread context delta + raw user message (+ gate hint when applicable) |

Hermes session retains turn-1 issue card, tools, and assistant history — follow-ups do **not** re-inject description, project, relations, or output rules.

---

## Code

| Symbol | Role |
|--------|------|
| `THREAD_CONTEXT_HEADER` / `FOOTER` | Same semantic markers as Slack gateway |
| `build_thread_context_block()` | Human comments only; watermark delta; excludes current message body |
| `build_native_turn_message()` | Created = issue card + thread + message; Prompted = anchor + thread + message |
| `build_conversation_text()` | Legacy mode only (unchanged format) |

---

## Verification

```bash
python3 -m pytest tests/test_slack_gateway_learnings.py tests/test_hermes_native_mode.py -v
```
