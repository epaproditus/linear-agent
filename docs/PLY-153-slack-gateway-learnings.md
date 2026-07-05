# PLY-153 — Linear agent should follow Slack gateway learnings

**Status:** implemented in prompts (2026-07-05)  
**Fixture:** [sample_slack_conversation.json](./fixtures/sample_slack_conversation.json)

---

## Source session

Exported Hermes Slack gateway session `20260704_222827_5dfe2f77` — **"Hermes Dashboard GUI Broken"** (164 messages, 14 user turns).

The user reported a broken "hermes-gui app" and Mac desktop connectivity. The agent spent ~55 terminal calls and 69 assistant messages before converging on the actual targets: `hermes-dashboard` (systemd service) **and** `nesquena/hermes-webui` (separate WebUI the user clarified in message 3).

---

## Metrics (from fixture analysis)

| Metric | Value | Takeaway |
|--------|-------|----------|
| User messages | 14 | — |
| Assistant messages | 69 | ~5× too chatty for the problem size |
| Tool messages | 80 | — |
| `terminal` tool calls | 55 | 40% were single-command probes |
| First `webui` mention in assistant text | message ~40 | User had to clarify product scope 3 turns earlier |

---

## Learnings → Linear agent changes

### 1. Clarify ambiguous terms before diagnosing

The user said "hermes-gui", "desktop app", and "dashboard" interchangeably. The agent assumed `hermes-dashboard.service` and burned ~20 terminal calls before the user pointed at `github.com/nesquena/hermes-webui`.

**Prompt rule added:** ask one short clarifying question when a product/service/UI name could refer to multiple things — before extensive shell probes.

### 2. Batch shell probes into single scripts

55 terminal invocations for a connectivity troubleshooting thread. Many were standalone `systemctl status`, `journalctl`, or `ss -tlnp` calls that could run in one script per round.

**Prompt rule added:** combine related diagnostics (status + ports + logs) in one terminal script per round.

### 3. Interim verbosity drowns the signal

69 assistant messages vs 14 user messages. Many were filler ("Let me dig in…", "Found it!") that add latency and token cost without new information.

**Linear advantage:** tool progress streams to the timeline; phase-2 finalize strips process narration from the final reply.

**Prompt rules added:**
- During tool loops: minimal assistant text; findings go to timeline + final reply.
- `LINEAR_OUTPUT_RULES`: explicitly ban process narration in the final message.

---

## Where encoded

| Constant / doc | Change |
|----------------|--------|
| `HERMES_WORK_STYLE` | Disambiguate, batch diagnostics, minimal interim text |
| `LINEAR_OUTPUT_RULES` | No process narration in final reply |
| `_build_finalize_prompt` | Already strips timeline duplication (unchanged) |
| `tests/test_slack_gateway_learnings.py` | Fixture metrics + prompt assertions |

---

## Verification

```bash
python3 -m pytest tests/test_slack_gateway_learnings.py -v
```
