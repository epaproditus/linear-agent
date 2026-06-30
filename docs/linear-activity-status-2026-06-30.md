# Linear activity — paste as comment or agent response

Copy everything below the line into a Linear issue comment, project update, or agent `response` activity.

---

## Status: Linear agent architecture redesign (complete — deploy native mode)

### Summary

We analyzed a real Hermes session export from **PLY-112** and redesigned the Linear adapter from an **orchestrator on top of Hermes** into a **thin adapter** (one continuous agent thread, like Cursor). The code is merged on `main` (#17–#20). **Action required:** set `HERMES_NATIVE_MODE=1` and restart `linear-agent`.

---

### What was wrong (PLY-112 session)

- **Wrong follow-up text:** 22 prompted turns used the gate *issue description* as the user message instead of the actual comment (“Why TCP?”, “Explain WireGuard”, etc.).
- **Prompt bloat:** Every follow-up re-sent the full comment thread (5 → 70 comments), hitting a **65KB truncation** (~238K tokens wasted).
- **7× finalize passes:** A second “rewrite for the user” LLM call ran on every turn even though tool progress was already on the timeline.
- **Gate overreach:** `delegate_task` and heavy research on a human-decision gate issue.

What worked: single Hermes session, correct VPS from sibling context, context-before-SSH.

---

### What we shipped

#### 1. Hermes-native mode (`HERMES_NATIVE_MODE=1`) — PR #17

- One `X-Hermes-Session-Id` per Linear agent session (no `:plan` / `:finalize` splits).
- Hermes `todo` → Linear Plan UI (no synthetic JSON planning).
- Minimal prompts: full context on first turn, delta on follow-ups.

#### 2. Prompted turn fixes — PR #18

- `resolve_user_request()` never uses issue description on follow-ups.
- Conversation **watermarks**: only new comments injected after each turn.
- Threaded comment deduplication.

#### 3. Conversational efficiency — PR #19

- **Skip finalize** on `prompted` turns (draft goes straight to user).
- **Gate issues** (`🚧 Gate — Human Required`): lightweight profile — recommend only, no deploy/PR/`delegate_task`; no auto In Review.
- Watermarks **persist** at `~/.linear-agent/conversation_watermarks.json`.

#### 4. Block relations — PR #20

- Fetches **blocked by / blocks / related / duplicate** from Linear GraphQL.
- Injects into every Hermes prompt.
- **Defers** new assignments when open blockers exist; follow-ups proceed with a warning (user override).
- `LINEAR_DEFER_ON_BLOCKERS=0` disables deferral.

#### 5. PLY-78 (earlier) — project context

- Rich project fields, sibling issues, guidance, context-before-action ordering.

---

### Issue state behavior (clarified)

| When | State change |
|------|----------------|
| Agent picks up issue | → **In Progress** (if was Todo/Backlog) |
| First turn finishes | → **In Review** (except gates and blocked deferrals) |
| You reply in agent thread | **No** state change — stays In Review |
| Gate issue | **No** auto In Review — you close the gate |
| Blocked issue (first mention) | **Deferred** — no work until blockers clear or you override in thread |

The agent does **not** set Done/Completed.

---

### Cursor vs Hermes

- **Hermes** (this agent on Linear): webhooks, issue context, timeline, state nudges, blockers.
- **Cursor** (cloud coding agent): separate surface — repo/PR work, no Linear session unless you bridge manually.

---

### Deploy

```bash
# .env
HERMES_NATIVE_MODE=1
# optional: LINEAR_DEFER_ON_BLOCKERS=true  (default)

# restart service
```

Full documentation: `docs/linear-agent-architecture-and-learnings.md` in the linear-agent repo.

---

### Remaining backlog (optional, not blocking deploy)

- Remove legacy planning path after soak
- Hermes session titles from Linear issue id
- `/v1/runs` lifecycle API
- Rich editor blocks beyond markdown `description`
- Slim DiscoveryTracker

---

### PRs

#17 Hermes-native · #18 prompted deltas · #19 finalize/gate/watermarks · #20 block relations — **all merged**.
