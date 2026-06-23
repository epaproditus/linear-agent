# PLY-40: Plan — Streaming Progress Updates While Hermes Works an Issue

**Status:** Draft for review  
**Author:** Hermes Agent  
**Date:** 2026-06-22  
**Issue:** [PLY-40](https://linear.app/epaphroditus/issue/PLY-40)

---

## 1. Problem Statement

When Hermes works a Linear issue (via @-mention / Agent Session), the user
currently sees:

- **Acknowledge:** "🤖 Hermes agent here! Processing the issue..."
- **Thinking...** (ephemeral, appears once at start)
- **Still thinking...** (repeated every 15s during long LLM calls)
- **Tool call activities:** Brief action labels like `🛠️ read_file` with
  truncated arguments
- **Final response:** A single wall of text showing what was done

Compare with other agents that show:
- Phase labels ("Analyzing issue...", "Researching codebase...",
  "Drafting response...")
- Progress milestones ("Found relevant file X", "Identified root cause Y",
  "Generated fix Z")
- Live-snippet updates from intermediate results

**The gap:** Hermes feels static. The activity stream updates are too sparse,
too generic ("Still thinking..." for extended periods), and don't convey the
*arc* of work — what phase we're in, what tools are running, how far along we
are.

This plan covers only **the agent's activity emission** — what Hermes reports
to Linear via Agent Session API activities. It does *not* cover token-by-token
streaming in the CLI/TUI (that already exists via `streaming: true` and
`tool_progress: all`).

---

## 2. Design Goals

1. **Feel alive** — The activity stream should update every 2-5s with
   meaningful phase/content progression, never "Still thinking..." for more
   than 10s straight.

2. **Phase awareness** — User should be able to tell what broad phase Hermes
   is in (understand → research → implement → review → respond) at a glance.

3. **Milestone visibility** — Key intermediate results should surface
   (found a file, matched a pattern, validated a change).

4. **Privacy** — Raw reasoning, internal prompts, credentials, and
   half-baked attempts must remain hidden.

5. **Non-disruptive** — Activity emission must not add latency to the actual
   work. Fires and forgets via the Linear API with no retry blocking.

6. **Configurable** — Platforms/teams that prefer fewer updates can opt in
   to a "quiet" mode.

---

## 2.5 Why Not Raw Streaming? (Technical Feasibility vs Design Tradeoffs)

A natural question: the Hermes API server already streams tokens via SSE — why not just
pipe raw reasoning directly into Linear activities?

### Raw Streaming IS Technically Feasible

The existing `_call_llm()` method (linear_agent.py lines 1517-1658) already:
1. Uses `stream: True` to get SSE chunks from the Hermes API
2. Detects tool calls mid-stream and emits `🛠️ tool_name` actions
3. Emits content snippets as `💭` thoughts every 10s during long generations

A "full raw mode" would simply skip all filtering — push every content delta, every
thinking token, every variant into the activity stream. The Linear API accepts these
at ~50-100ms per call, so throughput isn't a bottleneck.

### Why Raw Streaming Would Be Worse

**1. Linear activities are NOT a real-time streaming channel.**

Each activity is a discrete, persisted object — it creates a database row, triggers
webhooks, and appears in Linear's UI as a distinct event. The `ephemeral` flag hides
it from the permanent timeline but does NOT avoid the per-activity API call. Streaming
200+ activity objects per minute would:
- Inflate the activity timeline with thousands of tiny updates
- Trigger webhook events for every single one (unnecessary load)
- Cause UI flicker as Linear's activity poller refreshes

Compare: 10-30 PhaseTracker updates per session vs 200+ raw updates.

**2. Raw reasoning is mostly noise, not signal.**

An LLM generating 500-2000 tokens per step produces internal monologue like:
```text
"Hmm, that approach could work but what if we try... no wait, that ignores
the constraint about X. Let me reconsider. Actually if we look at it from
angle Y then..."
```

Showing this to the user is counterproductive. Every wrong turn, hesitation, and
rejected idea appears before the final conclusion. The result is less transparency,
not more — the signal-to-noise ratio is terrible.

**3. Security boundary erosion.**

The raw stream carries:
- Tool call arguments (file paths, code patterns, query strings)
- Intermediate errors (stack traces, 4xx/5xx responses, retry state)
- Potentially credentials if the LLM emits them before redaction
- Internal system prompts and architectural context

The PhaseTracker approach exposes only tool names + truncated arguments — a
deliberate security boundary that raw streaming would bypass.

**4. UI fragmentation across platforms.**

| Platform | Current behavior | Raw streaming |
|----------|-----------------|---------------|
| Linear UI | Activities as timeline items (not chat) | Would produce hundreds of refresh updates |
| CLI | `streaming: true` + `tool_progress: all` already works | Already has its own real-time view |
| Gateway (Telegram/Discord/WhatsApp) | Edits last message | Can't do true streaming — edit API is rate-limited |
| Agent Session API | Discrete activity events | No websocket/subscription for real-time |

A one-size-fits-all raw stream would be wrong for every platform except CLI — and
CLI already has its own solution.

### Comparison: PhaseTracker vs Raw Streaming

| Concern | PhaseTracker | Raw streaming |
|---------|-------------|---------------|
| Update frequency | Every 1.5-5s | Every 200ms |
| Activity objects per session | 10-30 | 200-500+ |
| Security boundary | Tool names + truncated args | Full CoT + tool args + errors |
| Platform fit (Linear UI) | Natural, readable timeline | UI jitter, too much noise |
| Platform fit (CLI) | Still works | Redundant (CLI already streams) |
| Platform fit (gateway) | Works (edits are infrequent) | Rate-limit violations |
| Signal vs noise | Phase headers + milestones | Everything including hesitation |
| Backward compatible | Additive, no breaking change | Would need opt-in toggle |

### Decision

Stick with the PhaseTracker approach as designed. If a power user genuinely wants
raw thinking tokens visible in Linear, add a `raw_reasoning: true` config flag that
pipes thinking-token deltas into thought activities — but document it as experimental
and not recommended.

---

## 3. What to Stream

### 3.1 Phase Headers (ephemeral action activities)

Every issue goes through a sequence of phases. Hermes should emit a phase
change as an `action` activity with `ephemeral=True` at phase transitions:

| Phase | Action Label | Example Body |
|-------|-------------|--------------|
| Starting | `🚀 Starting` | `Picked up PLY-40: Streaming progress updates` |
| Understanding | `📖 Understanding` | `Reading issue description, comments, and context...` |
| Researching | `🔍 Researching` | `Searching codebase for relevant files...` |
| Implementing | `⚡ Implementing` | `Making changes to linear_agent.py...` |
| Reviewing | `✅ Reviewing` | `Verifying changes are correct and complete...` |
| Responding | `💬 Responding` | `Drafting response to issue...` |

Phase transitions are driven by Hermes's own planning loop — the agent
decides what to do next and reports the phase change before starting.

### 3.2 Tool-In-Progress (ephemeral action activities)

When Hermes invokes a tool that takes noticeable time (>1s), emit a
descriptive action activity:

```
🛠️ Searching   → query: "streaming progress" in linear_agent.py
🛠️ Reading     → ~/linear-agent/linear_agent.py (lines 540-639)
🛠️ Writing     → Streaming progress plan to ~/linear-agent/plans/PLY-40.md
```

These are already partially emitted (the current code catches tool calls
from the LLM stream), but the content is too raw. Instead of truncated JSON
arguments, derive a human-readable description:

- `read_file(path)` → `📖 Reading {short_path}`
- `search_files(pattern)` → `🔍 Searching for "{pattern}" in {dir}`
- `write_file(path)` → `✏️ Writing {short_name}`
- `patch(path)` → `🔧 Editing {short_name}`
- `web_search(query)` → `🌐 Searching web for "{truncated_query}"`
- `delegate_task(goal)` → `🤖 Spawning subagent: {truncated_goal}`
- `terminal(command)` → `💻 Running: {truncated_command}`

### 3.3 Milestone Completions (non-ephemeral)

When a meaningful milestone is reached, emit a non-ephemeral action activity
that persists in the activity stream:

```
✅ Found root cause: stream.py line 142 — missing phase emission
✅ Created plan document at ~/linear-agent/plans/PLY-40.md
✅ Changes verified — all tests pass
```

These are not replaced by subsequent actions, so the user sees a trail of
accomplishments when they look back.

### 3.4 Live Thought Snippets (during long LLM calls)

The current code emits a thought snippet every 10s when the LLM is
generating. Keep this but improve the content:

- **Current:** `💭 ...{last 60 chars of generated text}`
- **Proposed:** `💭 Analyzing approach: {meaningful excerpt from what's being generated}`
  - Before making a decision: `💭 Considering approach: use phase tracking in _handle_analysis...`
  - After reaching conclusion: `💭 Decided: incremental phase emission via new PhaseTracker class`
  - Fallback (if no meaningful snippet): `💭 Generating response... (still working)`

### 3.5 Rich Response Activity

The final `response` activity should include visible progress metadata the
user can scroll back to see:

```markdown
## ✅ Done! PLY-40: Streaming progress updates

### What was done
- Created implementation plan at `~/linear-agent/plans/PLY-40.md`
- Analyzed current activity emission in `linear_agent.py`

### Work summary
- **Phases:** Understand → Research → Plan → Review
- **9 activities emitted** during processing
- **5 files examined**, **1 plan document created**
```

This gives post-hoc visibility into the work arc.

---

## 4. What NOT to Stream

| Category | Examples | Why |
|----------|----------|-----|
| Raw chain-of-thought | "First I should check if X, but then Y contradicts..." | Too much noise, internal |
| Failed attempts | "read_file failed: permission denied, retrying..." | User sees only the retry/success |
| Internal prompts | "You are Hermes, an autonomous agent..." | Security, privacy |
| Credentials | API keys, tokens, secrets | Security — redacted system-wide |
| Tool call JSON | `{"path":"/home/abe/..."}` | Raw JSON is unhelpful |
| Intermediate errors | 404s, connection resets, retries | Just the final resolved state |
| Subagent internals | Individual subagent tool calls | Summary of what subagent achieved only |
| Excessive detail | Every line read, every search result | Overwhelming — milestone summaries only |

**Guiding principle:** Stream what a collaborator would tell you in person.
Not every keystroke, not every hesitation — just what phase you're in, what
you're working on now, and what you've achieved.

---

## 5. Expected UX

### 5.1 Ideal Scenario (via Linear UI)

```
[+30s]  🤖 Hermes agent here! Processing PLY-40...
[+32s]  🚀 Starting — Picked up PLY-40: Streaming progress updates
[+34s]  📖 Understanding — Reading issue description and current activity emission code
[+38s]  🔍 Researching — Searching for activity emission patterns in linear_agent.py
[+42s]  🛠️ Searching -> "create_activity" in ~/linear-agent/
[+45s]  ✅ Found: activity system lives in linear_agent.py lines 540-638
[+48s]  📖 Reading — linear_agent.py (sections: activity emission, keepalive, streaming)
[+55s]  ✅ Identified: 3 areas to improve — phase headers, tool descriptions, milestone emissions
[+60s]  ⚡ Implementing — Drafting implementation plan for PLY-40
[+90s]  💬 Responding — Writing up plan for review
[+95s]  ✅ Done! Created plan at ~/linear-agent/plans/PLY-40.md
```

Every 2-5 seconds there's a visible update. No gap longer than 10s without
some activity (falling back to a "Still working..." thought if nothing else
fits).

### 5.2 During Long LLM Calls

```
[+60s]  💭 Analyzing: comparing current activity emission with other agent strategies...
[+75s]  💭 Decided: use PhaseTracker class to manage progress state
[+90s]  💭 Developing approach: incremental phases with heartbeat fallback...
[+105s] 💭 Drafting response...
```

### 5.3 Quiet Mode (configurable)

For teams that prefer fewer updates, a `progress: sparse` mode:
- Phase headers only (no per-tool updates)
- Milestones only (no thought snippets)
- Keepalive every 30s instead of 10s

---

## 6. Technical Approach

### 6.1 PhaseTracker: A Lightweight State Machine

Introduce a `PhaseTracker` class in `linear_agent.py`:

```python
class PhaseTracker:
    """Tracks and emits progress phase transitions during issue work."""

    PHASES = [
        ("starting", "🚀 Starting"),
        ("understanding", "📖 Understanding"),
        ("researching", "🔍 Researching"),
        ("implementing", "⚡ Implementing"),
        ("reviewing", "✅ Reviewing"),
        ("responding", "💬 Responding"),
    ]

    def __init__(self, session_id: str, linear_client: LinearClient):
        self.session_id = session_id
        self.linear = linear_client
        self.current_phase = None
        self.last_emit = 0.0
        self.milestones: list[str] = []

    async def enter_phase(self, phase: str, description: str = ""):
        """Transition to a new phase, emit as ephemeral action."""
        self.current_phase = phase
        label, icon = self._phase_info(phase)
        await self.linear.send_action(
            self.session_id, label, description,
            ephemeral=True,
        )
        self.last_emit = time.monotonic()

    async def milestone(self, msg: str):
        """Emit a persistent milestone."""
        self.milestones.append(msg)
        await self.linear.send_action(
            self.session_id, "✅ " + msg.split(":")[0], msg,
            ephemeral=False,  # NOT ephemeral — persists in timeline
        )
        self.last_emit = time.monotonic()

    async def emit_activity(self, kind: str, body: str,
                            ephemeral: bool = True):
        """General-purpose activity emission."""
        await self.linear.create_activity(
            self.session_id,
            ActivityType.action if kind != "thought" else ActivityType.thought,
            body=body, ephemeral=ephemeral,
        )
        self.last_emit = time.monotonic()

    async def heartbeat(self, force: bool = False):
        """Emit keepalive if enough time has passed since last emission."""
        # No-op: heartbeat is handled by the keepalive task below
        pass
```

### 6.2 Enhanced Keepalive with Progress

Replace the current `_keep_session_alive()` with a smarter version that
carries context:

```python
async def _keep_session_alive(self, session_id: str,
                               phase_tracker: PhaseTracker | None = None) -> None:
    """Periodically emit keepalive activities, incorporating current phase."""
    messages = [
        "Still working on it...",
        "Processing — this is taking a bit longer than expected",
        "Still here! Running some analysis...",
        "Working through the details...",
        "Taking a bit longer — good things take time!",
        "Still crunching...",
    ]
    idx = 0
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL_S)
        try:
            msg = messages[idx % len(messages)]
            idx += 1
            phase = phase_tracker.current_phase if phase_tracker else ""
            prefix = f"[{phase}] " if phase else ""
            await self.linear.create_activity(
                session_id, ActivityType.thought,
                body=f"{prefix}{msg}", ephemeral=True,
            )
        except Exception:
            pass
```

### 6.3 Tool Call Activity Enhancement

In `_call_llm()`, when a tool call is detected from the stream, translate
it into a human-readable description:

```python
_TOOL_LABELS = {
    "read_file": ("📖", "Reading"),
    "write_file": ("✏️", "Writing"),
    "search_files": ("🔍", "Searching"),
    "patch": ("🔧", "Editing"),
    "web_search": ("🌐", "Searching web"),
    "web_extract": ("📄", "Fetching URL"),
    "terminal": ("💻", "Running"),
    "delegate_task": ("🤖", "Delegating"),
    "execute_code": ("🐍", "Running code"),
}

def _describe_tool_call(name: str, args: dict) -> str:
    """Generate a human-readable description of a tool call."""
    icon, verb = _TOOL_LABELS.get(name, ("🛠️", name))
    # Key arguments that make good descriptions
    if name == "search_files":
        return f"{verb} for \"{args.get('pattern', '')[:60]}\""
    elif name in ("read_file",):
        return f"{verb} {args.get('path', '')[:80]}"
    elif name in ("write_file", "patch"):
        return f"{verb} {args.get('path', '')[:80]}"
    elif name == "web_search":
        return f"{verb} for \"{args.get('query', '')[:80]}\""
    elif name == "terminal":
        cmd = args.get("command", "")[:60]
        return f"{verb}: {cmd}"
    elif name == "delegate_task":
        goal = args.get("goal", "")[:80]
        return f"{verb}: {goal}"
    else:
        return f"{verb}({name})"
```

### 6.3b Tool Result Emission (Cursor-Inspired)

After each tool completes, emit a concise result summary when the output is short and meaningful. This closes the main gap with Cursor's model (Cursor shows tool *results* inline, not just tool *calls*).

```python
_TOOL_RESULT_SUMMARIZERS: dict[str, Callable[[Any], str | None]] = {
    "search_files": lambda r: (
        f"Found {r['total_count']} matches"
        if isinstance(r, dict) and r.get("total_count", 0) > 0
        else None
    ),
    "web_search": lambda r: (
        f"Found {len(r.get('results', []))} results"
        if isinstance(r, dict) else None
    ),
}

def _summarize_tool_output(name: str, result: Any, args: dict) -> str | None:
    \"\"\"Return a brief milestone-worthy summary, or None if not meaningful.\"\"\"
    if name == "search_files":
        matches = result.get("total_count", 0) if isinstance(result, dict) else 0
        if matches > 0:
            pattern = args.get("pattern", "")[:40]
            return f"Found {matches} match{'es' if matches != 1 else ''} for \"{pattern}\""
    elif name == "read_file":
        path = args.get("path", "")
        lines = result.count("\\n") + 1 if isinstance(result, str) else 0
        if lines > 0:
            return f"Read {path} ({lines} lines)"
    elif name == "web_search":
        results = result.get("results", []) if isinstance(result, dict) else []
        if results:
            return f"Found {len(results)} web results"
    elif name == "execute_code":
        status = result.get("status", "")
        if status == "success":
            return "Code executed successfully"
    return None  # No summary worth emitting
```

This runs in the main loop after each tool call completes. If a summary is returned and <200 chars, emit it as a non-ephemeral milestone:

```python
summary = _summarize_tool_output(tool_name, tool_result, tool_args)
if summary and len(summary) < 200:
    await tracker.milestone(summary)
```

Too-noisy results (large file reads, terminal output, complex data) return `None` naturally via the guard conditions above.

### 6.4 Phase Detection Heuristics

The agent needs to know what phase it's in. This is driven by the tool call
stream and the phase transition logic in `_handle_analysis()`:

```python
# In _handle_analysis, before calling LLM:
tracker = PhaseTracker(session_id, self.linear)
await tracker.enter_phase("understanding", f"Reading issue {identifier}")

# Then, periodically during LLM processing, the stream parser
# detects tool calls and infers phase:
async for chunk in stream:
    tool = detect_tool_call(chunk)
    if tool:
        phase = infer_phase_from_tool(tool.name)
        if phase and phase != tracker.current_phase:
            await tracker.enter_phase(phase, tool.description)
```

Phase inference from tool calls:

| Tool | Inferred Phase |
|------|---------------|
| `read_file`, `search_files`, `web_search`, `web_extract` | `researching` |
| `write_file`, `patch`, `terminal` (build commands) | `implementing` |
| `terminal` (test commands), `read_file` (verification read) | `reviewing` |
| `delegate_task` | `implementing` |
| LLM text generation (no tools for a while) | `responding` |

This uses the *first* tool call in a batch to infer phase — once a phase is
set, it doesn't change until a tool call from a different category appears.

### 6.5 Activity Rate Limiting

To avoid flooding Linear's API (rate limit: 5,000 req/h per key), enforce
a minimum interval between activity emissions:

```python
MIN_ACTIVITY_INTERVAL = 1.5  # seconds between any activity
MILESTONE_INTERVAL = 3.0     # minimum between milestones
```

The `PhaseTracker.emit_activity()` method checks these and skips if the
interval hasn't elapsed (except for phase transitions, which always fire).

### 6.6 No Changes to Agent Session API

The integration stays 100% within the existing Agent Session API:
- Activities are the only communication channel
- `ephemeral=True` activities are replaced by the next one (Linear handles
  this on the UI side — the activity slot updates in-place)
- `ephemeral=False` activities accumulate in the timeline
- No polling, no websocket, no new API surface

---

## 7. Files Changed

| File | Change |
|------|--------|
| `~/linear-agent/linear_agent.py` | Add `PhaseTracker` class, enhance `_call_llm` tool descriptions, improve keepalive, add phase inference, add milestone emission |
| `~/linear-agent/plans/PLY-40.md` | This plan document (keep as reference) |

No new files. No config changes. No new dependencies.

---

## 8. Risks & Tradeoffs

### 8.1 Activity Flooding

**Risk:** Too many activity emissions could hit Linear's 5,000 req/h rate
limit, especially during complex multi-tool tasks.

**Mitigation:** Enforce `MIN_ACTIVITY_INTERVAL = 1.5s` — at 2,400 req/h,
well under the 5,000 limit. Phase transitions are exempt from the interval
(a single extra activity per phase change is negligible).

### 8.2 Activity vs. Real Progress

**Risk:** Emitting activities could add latency if done synchronously.

**Mitigation:** Activity emission is fire-and-forget (the current
implementation already uses async HTTP without awaiting retries). Phase
tracker emits are non-blocking — the main work loop never waits for an
activity to be confirmed.

### 8.3 Phase Misclassification

**Risk:** Heuristic phase detection (inferring phase from tool name) could
misclassify. E.g., `read_file` could be used in any phase.

**Mitigation:** Phase transitions are suggestions, not commands. The agent
can explicitly call `tracker.enter_phase()` to override. Misclassification
is a UX issue (wrong icon/phase shown briefly), never a correctness issue.
Next tool call in a different category corrects it.

### 8.4 Noise in Activity Timeline

**Risk:** Non-ephemeral milestones could clutter the activity timeline.

**Mitigation:** Milestones are intentionally kept to major achievements
only (file created, root cause found, tests passing). The `_handle_analysis`
path currently emits 2-4 persistent activities total; with this change,
expect 4-8 per issue — still very lean.

### 8.5 Backward Compatibility

**Risk:** Existing agent sessions in progress when code is deployed.

**Mitigation:** All changes are additive — new activity emissions alongside
existing ones. No change to activity schema or API calls. Running sessions
that don't use `PhaseTracker` continue with the old behavior. A restart
picks up the new code.

---

## 9. Fallback Behavior

| Condition | Behavior |
|-----------|----------|
| Linear API rate-limited (429) | Activity is dropped silently; work continues uninterrupted |
| Activity creation fails | Logged at DEBUG level; no retry; work continues |
| Phase tracker not initialized | Falls back to current behavior (keepalive only) |
| Unknown phase requested | Falls back to "⚙️ Working" (generic phase label) |
| Network error posting activity | Logged and ignored; work continues |
| Tool name not in `_TOOL_LABELS` | Falls back to `🛠️ {tool_name}()` with truncated args |
| Zero activities for >30s | Keepalive fires "Still working..." (unchanged from current) |

The invariant: **activity emission is always optional.** The core task
processing (LLM calls, tool execution, response generation) never blocks
on activity delivery.

---

## 10. Documentation

### 10.1 Updated Module Docstring (linear_agent.py)

The existing docstring at the top of `linear_agent.py` should be updated
to mention the activity emission system:

```
Architecture
────────────
Linear Webhook POST → HMAC verify → IP allowlist → Event router
  → Background asyncio Task
    → Acknowledge (thought activity within 10s — required by Linear)
    → Parse promptContext (issue, comments, guidance)
    → Initialize PhaseTracker (monitors & emits progress)
    → Process task (analyze, research, code)
      → Emit phase transitions (📖 Understanding → 🔍 Researching → etc.)
      → Emit tool-in-progress activities for visible tools
      → Emit milestone activities for key achievements
      → Background keepalive during long LLM calls
    → Emit response activity with result + work summary
    → Update issue (comment, status, assignee)
```

### 10.2 PhaseTracker Docstring

Add a thorough docstring to `PhaseTracker`:

```python
class PhaseTracker:
    """Emits visible progress updates to Linear Agent Session.

    Uses Linear's activity system (thought/action/response) to show
    the user what phase Hermes is in, what tool is running, and what
    milestones have been achieved — without exposing internal reasoning.

    Key behaviors:
    - Phase transitions (understanding → researching → etc.) are
      ephemeral actions replaced by the next phase.
    - Tool-in-progress activities show what tool is running and on
      what input, using human-readable descriptions not raw JSON.
    - Milestones are non-ephemeral actions that persist in the
      timeline, giving the user a visible trail of accomplishments.
    - All emissions are fire-and-forget — never block the main work.
    - Rate-limited to MIN_ACTIVITY_INTERVAL between emissions to
      avoid flooding Linear's API.
    """
```

### 10.3 Configuration Reference

Add a config section documenting acceptable activity frequency:

```yaml
# ~/linear-agent/config.yaml (or .env)
# Activity emission tuning:

# How often the keepalive fires during LLM processing (seconds)
# KEEPALIVE_INTERVAL_S=15

# Minimum interval between activity emissions (seconds)
# MIN_ACTIVITY_INTERVAL_S=1.5

# Minimum interval between milestone emissions (seconds)
# MILESTONE_INTERVAL_S=3.0

# Progress verbosity: "full" (default), "sparse" (phase+milestones only)
# PROGRESS_VERBOSITY=full
```

### 10.4 User-Facing Docs (for README or Linear Doc)

Create a brief user-facing doc in
`~/linear-agent/docs/progress-visibility.md`:

```markdown
# Progress Visibility

When Hermes works your issue, it shows visible progress in the
Linear activity timeline:

1. **Phase badges** — See what phase Hermes is in:
   📖 Understanding → 🔍 Researching → ⚡ Implementing → 💬 Responding

2. **Tool indicators** — Know what command is running:
   "📖 Reading linear_agent.py" or "🌐 Searching web for..."

3. **Milestones** — See key achievements:
   "✅ Root cause found: line 142 missing phase emission"

4. **No internal reasoning** — You see progress, not thinking.

## Configuration

Set `PROGRESS_VERBOSITY=sparse` in your environment to see only
phase badges and milestones (no per-tool updates).
```

---

## 11. Implementation Order

| Step | Description | Estimated Effort |
|------|-------------|-----------------|
| 1 | Add `PhaseTracker` class with phase management and rate limiting | 1-2 hours |
| 2 | Integrate `PhaseTracker` into `_handle_analysis()` — initialize, emit phases | 1 hour |
| 3 | Enhance tool call descriptions in `_call_llm()` — human-readable labels | 1 hour |
| 4 | Add phase inference from tool stream | 30 min |
| 5 | Enhance keepalive with progress context | 30 min |
| 6 | Add milestone emission in `_handle_analysis()` and `_handle_dev_task()` | 1 hour |
| 7 | Add fallback label for unknown tools | 15 min |
| 8 | Test with real issue workflows | 1 hour |
| 9 | Write documentation | 30 min |
| **Total** | | **~6-8 hours** |

---

## 12. Success Criteria

The implementation is complete when:

1. **Phase transitions visible** — Every issue handling session shows clear
   phase progression (📖 → 🔍 → ⚡ → 💬) in the Linear activity timeline, with
   no more than 10s gap between updates during active processing

2. **Tool calls readable** — Activities show "📖 Reading file X" instead of
   "🛠️ read_file" with JSON args

3. **Milestones persist** — Key accomplishments (files created, root causes
   found, changes verified) appear as non-ephemeral activities

4. **Rate safety** — >2,400 req/h headroom on Linear API rate limit

5. **Fallback clean** — API errors during activity emission never crash or
   delay the actual task processing

6. **No regression** — "Still thinking..." keepalive still fires if no other
   activity has been emitted within 15s

---

*This plan is a proposal for review. Once approved, implementation can begin.*
