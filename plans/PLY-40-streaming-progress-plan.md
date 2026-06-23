# PLY-40: Plan — Streaming Progress Updates While Hermes Works an Issue

**Status:** Draft for review
**Author:** Hermes Agent
**Date:** 2026-06-22 (rewritten)
**Issue:** [PLY-40](https://linear.app/epaphroditus/issue/PLY-40)

---

## 1. Problem Statement

When Hermes works a Linear issue (via @-mention / Agent Session), the user
currently sees:

- **Acknowledge:** A brief "Hermes agent here! Processing the issue..." thought
- **Tool call activities:** Brief labels like `read_file` with raw arguments
- **Keepalive:** "Still thinking..." repeated every 15s during LLM calls
- **Final response:** A single wall of text showing what was done

Compare with Cursor's agent mode, where the work product itself materializes
in real time — you see lines of code being read, diffs appearing, terminal
output scrolling, and search results arriving. The dynamic feeling comes from
**watching the answer being assembled**, not from watching status badges update.

**The gap:** Hermes feels static because it announces intent ("Thinking...",
"read_file") instead of revealing findings ("Found streaming code at line 1523",
"Identified 3 gaps in current approach"). The activity stream carries no new
information between the acknowledge and the final response — just noise.

**This is not a problem of frequency.** Emitting more announcements ("Now in
researching phase! Still thinking!") would make Hermes more verbose, not more
dynamic. The fix is to change *what* gets emitted: from forward-looking status
to backward-looking results.

---

## 2. Design Goals

1. **Each activity carries new information** — Between "Hermes agent here!" and
   the final response, every emitted activity should tell the user something
   they didn't know before. A discovery, a decision, a result.

2. **The timeline tells a story** — Scrolling back through activities should
   show the arc of work: issue understood → code located → gaps identified →
   approach chosen → implementation created → verified.

3. **No dead air** — The user should never see a gap longer than 10s without
   some visible update. When no new finding is ready, show a contextual
   keepalive that references the current investigation direction.

4. **Privacy** — Raw chain-of-thought reasoning, internal prompts, credentials,
   failed attempts, and subagent internals remain hidden. The user sees
   curated findings, not the full reasoning trace.

5. **Non-disruptive** — Activity emission must never block or delay the actual
   work. Fire-and-forget over the Linear API with no retry blocking.

6. **Configurable** — A "sparse" mode shows fewer updates for teams that
   prefer quiet.

---

## 2.5 Why Not Raw Streaming? (Technical Feasibility vs Design Tradeoffs)

A natural question: the Hermes API server already streams tokens via SSE — why
not pipe raw reasoning directly into Linear activities?

### Raw Streaming IS Technically Feasible

The existing `_call_llm()` method (lines 1523-1665) already:
1. Uses `stream: True` to get SSE chunks from the Hermes API
2. Detects tool calls mid-stream and emits action activities
3. Emits content snippets every 10s during long generations

A "full raw mode" would push every content delta, every thinking token, every
tool variant into the activity stream. The Linear API accepts these at
~50-100ms per call.

### Why Raw Streaming Would Be Worse

**1. Linear activities are NOT a real-time streaming channel.**

Each activity is a discrete, persisted object — it creates a database row,
triggers webhooks, and appears in the UI as a distinct event. The `ephemeral`
flag hides it from the permanent timeline but does NOT avoid the per-activity
API call. Streaming 200+ activities per minute would:
- Inflate the activity timeline with thousands of tiny updates
- Trigger unnecessary webhook events
- Cause UI flicker as Linear's activity poller refreshes

A result-oriented approach emits 8-20 activities per session. Raw streaming
would emit 200-500+.

**2. Raw reasoning is mostly noise.**

An LLM generating 500-2000 tokens per step produces internal monologue like:
```
"Hmm, that approach could work but what if we try... no wait, that ignores
the constraint about X. Let me reconsider..."
```

Showing every wrong turn and hesitation is counterproductive. The signal-to-
noise ratio is terrible.

**3. Security boundary erosion.**

The raw stream carries tool call arguments (file paths, patterns, queries),
intermediate errors (stack traces, retry state), and potentially credentials.
The result-oriented approach exposes only curated findings — a deliberate
security boundary that raw streaming bypasses.

**4. UI fragmentation across platforms.**

| Platform | Current behavior | Raw streaming would |
|----------|-----------------|---------------------|
| Linear UI | Activities as timeline items | Hundreds of refresh updates, UI jitter |
| CLI | Already has real-time streaming via `streaming: true` | Redundant |
| Gateway (Telegram/Discord) | Edits last message | Rate-limited, can't do true streaming |
| Agent Session API | Discrete activity events | No subscription model for real-time |

### The Real Difference

Raw streaming addresses *frequency* but not *content*. Even if you stream every
token, you're still showing the process, not the findings. The result-oriented
model addresses the actual problem: making each activity informative on its own.

---

## 3. What to Stream

### 3.1 Discovery Activities (non-ephemeral, persist in timeline)

After each meaningful step completes, emit a non-ephemeral action that records
what was discovered or accomplished:

```
Hermes agent here! Processing PLY-40...
Understood: issue PLY-40 needs visible progress during issue work
Found: streaming infrastructure in linear_agent.py lines 1523-1665
Identified: 3 gaps — no result-oriented content, raw tool JSON, generic keepalive
Decided: emit findings instead of announcements; result-oriented model
Created: implementation plan at ~/linear-agent/plans/PLY-40.md
Responding with plan summary...
```

Each activity reveals a new finding. The timeline tells a story of discovery,
not a list of commands run. The user watches the answer being assembled piece
by piece.

**Guide for what qualifies as a discovery:**
- **Found:** A piece of information located during investigation
  - "Found: streaming code spans lines 1523-1665 in linear_agent.py"
  - "Found: create_activity accepts action_label, action_param, action_result"
  - "Found: cursor agent shows inline diffs and live terminal output"
- **Identified:** A gap, pattern, or relationship recognized
  - "Identified: all current activities are forward-looking (intent, not result)"
  - "Identified: tool call arguments contain file paths — too sensitive to expose raw"
- **Decided:** A choice made between alternatives
  - "Decided: result-oriented model over phase badges — shows progress through findings"
  - "Decided: fire-and-forget emission, never block on activity delivery"
- **Created:** An artifact produced
  - "Created: implementation plan at ~/linear-agent/plans/PLY-40.md"
  - "Created: PhaseTracker class with rate-limited emission"
- **Verified:** A validation completed
  - "Verified: all existing tests pass after changes"
  - "Verified: rate limit headroom at 2,400 req/h"

### 3.2 In-Progress Activities (ephemeral, replaced by next activity)

During long-running operations, emit brief ephemeral activities that indicate
what's being investigated right now:

```
Searching for activity emission patterns in linear_agent.py...
```

These are replaced by the next ephemeral activity (or by a discovery activity)
and do not clutter the permanent timeline. They serve to prevent dead air.

Content guidelines:
- Describe what's being investigated, not what tool is running
- "Searching for create_activity usage patterns" not "Running search_files"
- "Reading streaming implementation in _call_llm" not "Reading file"
- Keep under 100 chars. These are placeholders, not contributions.

### 3.3 Contextual Keepalive

Replace the static "Still thinking..." with a message that references the
current investigation direction:

```
Still investigating activity emission patterns...
Still reviewing streaming code structure...
Still analyzing Cursor agent behavior...
```

The keepalive picks up context from the last discovery or in-progress activity.
If no context is available, fall back to "Still working on it..." — which is
at least honest about the lack of context.

### 3.4 Rich Response Activity

The final `response` activity includes a work summary that makes the discovery
trail visible post-hoc:

```
## Done: PLY-40 — Streaming Progress Updates

### What was done
- Created implementation plan at ~/linear-agent/plans/PLY-40.md
- Analyzed current activity emission in linear_agent.py
- Compared against Cursor agent's real-time progress model

### Work summary
- 5 files examined
- 3 gaps identified
- 1 plan document created
- 12 activities emitted during processing
```

---

## 4. What NOT to Stream

| Category | Examples | Why |
|----------|----------|-----|
| Raw chain-of-thought | "First I should check X, but Y contradicts..." | Too much noise, internal reasoning |
| Failed attempts | "read_file failed: permission denied, retrying..." | User sees only resolved state |
| Internal prompts | "You are Hermes, an autonomous agent..." | Security, privacy |
| Credentials | API keys, tokens, secrets | Security — redacted system-wide |
| Tool call JSON | `{"path":"/home/abe/..."}` | Raw JSON is noise, not signal |
| Intermediate errors | 404s, connection resets | Just the final resolved state |
| Subagent internals | Individual subagent tool calls | Summary of subagent achievement only |
| Excessive detail | Every line read, every search result | Milestone summaries only |

**Guiding principle:** Each activity must contain information the user didn't
have before. If an activity only says what the agent is about to do, it's
noise. If it says what the agent found or accomplished, it's signal.

---

## 5. Expected UX

### 5.1 Ideal Scenario (via Linear UI)

```
[+1s]  Hermes agent here! Processing PLY-40...
[+5s]  Understood: issue PLY-40 needs visible progress during issue work
[+10s] Found: streaming infrastructure in linear_agent.py lines 1523-1665
[+15s] Identified: 3 gaps — no result-oriented content, raw tool JSON, generic keepalive
[+18s] Decided: emit findings instead of announcements; result-oriented model
[+25s] Still reviewing plan structure...
[+30s] Created: implementation plan at ~/linear-agent/plans/PLY-40.md
[+32s] Responding with plan summary...
[+35s] Done: PLY-40 — plan ready for review (5 files, 3 gaps, 1 plan, 12 activities)
```

Every 2-10 seconds there's a visible update that teaches the user something.
No gap longer than 10s without activity (falling back to contextual keepalive).

### 5.2 During Long LLM Calls

```
[+60s] Analyzing: comparing current activity model with other agents...
[+75s] Decided: result-oriented model fits Linear's activity paradigm
[+90s] Structuring: organizing findings into plan sections...
[+105s] Still generating response...
```

### 5.3 Sparse Mode (configurable)

For teams that prefer fewer updates, a `progress: sparse` mode:
- Discovery activities only (no in-progress activities)
- Keepalive every 30s instead of 15s
- No thought snippets during LLM calls

---

## 6. Technical Approach

### 6.1 DiscoveryTracker: Lightweight State Machine

Introduce a `DiscoveryTracker` class that manages discovery emission and rate
limiting. Unlike a phase tracker, this class has no concept of "current phase"
— it just remembers the last emission time and provides helpers for emitting
different kinds of findings.

```python
@dataclass
class DiscoveryTracker:
    """Tracks and emits discovery activities during issue work.

    Unlike a phase tracker, this has no concept of phase. It provides
    helpers for emitting four kinds of findings (found, identified,
    decided, created) and rate-limits emission to avoid flooding
    Linear's API.

    Key invariant: every non-keepalive activity carries information
    the user didn't have before.
    """

    session_id: str
    linear: LinearClient
    last_emit: float = 0.0
    activity_count: int = 0
    _keepalive_ctx: str = ""

    MIN_ACTIVITY_INTERVAL = 1.5  # seconds between activities
    MILESTONE_INTERVAL = 3.0     # seconds between persistent milestones

    async def found(self, detail: str) -> bool:
        """Emit a discovery: something located during investigation."""
        return await self._emit("Found", detail, ephemeral=False)

    async def identified(self, detail: str) -> bool:
        """Emit a discovery: a gap, pattern, or relationship recognized."""
        return await self._emit("Identified", detail, ephemeral=False)

    async def decided(self, detail: str) -> bool:
        """Emit a discovery: a choice made between alternatives."""
        return await self._emit("Decided", detail, ephemeral=False)

    async def created(self, detail: str) -> bool:
        """Emit a discovery: an artifact produced."""
        return await self._emit("Created", detail, ephemeral=False)

    async def verified(self, detail: str) -> bool:
        """Emit a discovery: a validation completed."""
        return await self._emit("Verified", detail, ephemeral=False)

    async def in_progress(self, description: str) -> bool:
        """Emit an ephemeral in-progress indicator."""
        self._keepalive_ctx = description
        return await self._emit("", description, ephemeral=True)

    async def _emit(self, kind: str, detail: str, ephemeral: bool) -> bool:
        """Rate-limited activity emission. Skips if interval hasn't elapsed."""
        now = time.monotonic()
        if ephemeral:
            sel_interval = 0.5  # in-progress activities are cheap
        else:
            sel_interval = self.MILESTONE_INTERVAL
        if now - self.last_emit < sel_interval:
            return False
        self.last_emit = now
        self.activity_count += 1

        body = f"{kind}: {detail}" if kind else detail
        return await self.linear.send_action(
            self.session_id,
            label=kind or "progress",
            param=detail[:200],
            body=body,
            ephemeral=ephemeral,
        )

    def keepalive_context(self) -> str:
        """Return the last in-progress description for keepalive."""
        return self._keepalive_ctx or "Still working on it..."
```

### 6.2 Enhanced Keepalive with Discovery Context

Replace the current `_keep_session_alive()` with a version that carries
context from the last in-progress message:

```python
async def _keep_session_alive(self, session_id: str,
                               tracker: DiscoveryTracker | None = None) -> None:
    """Periodically emit keepalive activities with discovery context."""
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL_S)
        try:
            ctx = tracker.keepalive_context() if tracker else "Still thinking..."
            await self.linear.create_activity(
                session_id,
                ActivityType.thought,
                body=ctx,
                ephemeral=True,
            )
        except Exception:
            pass
```

This replaces the rotating list of generic messages ("Still thinking...",
"Still here! Running some analysis...", etc.) with a single context-aware
message that references the current investigation direction.

### 6.3 Tool Result → Discovery Mapping

Instead of describing the tool call itself, derive a discovery statement from
the tool's output. This is the core shift from announcements to results.

```python
_DISCOVERY_EXTRACTORS: dict[str, Callable[[dict, Any], str | None]] = {
    "read_file": lambda args, result: (
        f"Read {args.get('path', '')} ({result.count(chr(10)) + 1} lines)"
        if isinstance(result, str) and len(result) > 0
        else None
    ),
    "search_files": lambda args, result: (
        f"Found {result.get('total_count', 0)} matches for"
        f" '{args.get('pattern', '')[:40]}' in {args.get('path', '.')}"
        if isinstance(result, dict) and result.get("total_count", 0) > 0
        else "No matches found"
    ),
    "web_search": lambda args, result: (
        f"Found {len(result.get('results', []))} web results for"
        f" '{args.get('query', '')[:50]}'"
        if isinstance(result, dict) and result.get("results")
        else "No web results found"
    ),
    "web_extract": lambda args, result: (
        f"Fetched content from {args.get('urls', ['?'])[0][:60]}"
        f" ({len(result)} chars)"
        if isinstance(result, str)
        else None
    ),
    "execute_code": lambda args, result: (
        "Code executed successfully"
        if isinstance(result, dict) and result.get("status") == "success"
        else None
    ),
}


def extract_discovery(tool_name: str, args: dict, result: Any) -> str | None:
    """Return a discovery statement from a completed tool call, or None."""
    extractor = _DISCOVERY_EXTRACTORS.get(tool_name)
    if extractor is None:
        return None
    try:
        return extractor(args, result)
    except Exception:
        return None
```

This runs synchronously after each tool call completes in the main loop:

```python
# In the main processing loop:
discovery = extract_discovery(tool_name, tool_args, tool_result)
if discovery:
    await tracker.found(discovery)
```

If no discovery can be derived (tool output too complex, too verbose, or no
extractor registered), nothing is emitted — the keepalive timer continues
ticking and the next tool call picks up.

### 6.4 Deriving Discoveries from LLM Stream

The most valuable discoveries come not from tool calls but from the LLM's
streaming output — the actual reasoning about what was found. The current
code already captures content deltas. The improvement is to replace the
last-60-chars snippet with extraction of meaningful finding statements.

**Mechanism:** After the LLM completes a thought segment (detected by pauses
in the stream, sentence boundaries, or newlines followed by decisions), check
if the accumulated content contains a finding. Finding patterns include:

- "Found:" or "found that" — a discovery
- "Identified:" — a gap or pattern
- "Decided:" or "going with" or "approach:" — a decision
- "Created:" or "wrote" — an artifact
- "Root cause:" or "the issue is" — a diagnosis

```python
_FINDING_PATTERNS = re.compile(
    r'(Found|Identified|Decided|Created|Root cause|The issue is|Going with)',
    re.IGNORECASE,
)

def extract_llm_finding(accumulated: str, known_findings: set[str]) -> str | None:
    """Check accumulated LLM output for a new finding statement."""
    lines = accumulated.split("\n")
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped or len(stripped) < 15:
            continue
        if _FINDING_PATTERNS.search(stripped):
            # Normalize: trim trailing period, cap length
            finding = stripped.rstrip(".").strip()[:200]
            if finding not in known_findings:
                return finding
    return None
```

This runs every 10s during LLM streaming (same cadence as the current thought
snippet). If a new finding is extracted, emit it as a discovery activity.

### 6.5 Activity Rate Limiting

Same approach as the original plan — enforce minimum intervals to avoid
flooding Linear's API (5,000 req/h per key):

```python
MIN_ACTIVITY_INTERVAL = 1.5   # seconds between any activity
MILESTONE_INTERVAL = 3.0      # seconds between persistent discoveries
```

At 1.5s minimum between activities, the theoretical max is 2,400 req/h —
well under Linear's 5,000 limit.

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
| `linear_agent.py` | Add `DiscoveryTracker` class, replace content-tail snippets with finding extraction, enhance keepalive with discovery context, add discovery extraction from tool results |
| `plans/PLY-40-streaming-progress-plan.md` | This plan document (keep as reference) |

No new files. No config changes. No new dependencies.

---

## 8. Risks & Tradeoffs

### 8.1 Activity Flooding

**Risk:** Too many discoveries could hit Linear's 5,000 req/h rate limit.

**Mitigation:** Enforce `MIN_ACTIVITY_INTERVAL = 1.5s` — at 2,400 req/h, well
under the limit. Discovery extractors return `None` for non-informative
results, keeping volume naturally low. Expected: 8-20 activities per session.

### 8.2 Activity vs. Real Progress

**Risk:** Emitting activities could add latency if done synchronously.

**Mitigation:** Activity emission is fire-and-forget (the current implementation
already uses async HTTP without awaiting retries). Discovery tracker emits are
non-blocking — the main work loop never waits for confirmation.

### 8.3 Missing Discoveries

**Risk:** The tool result extractors or LLM finding patterns could miss
meaningful results, making the agent feel silent again.

**Mitigation:** This is a non-critical UX issue — the final response still
contains the full result. The extractor set is extensible (add new tool
mappings in a dict). The LLM finding extraction runs every 10s as a safety
net: even if all extractors miss, the LLM's own output may yield a finding.
If everything misses, the contextual keepalive still fires.

### 8.4 Discovery Quality

**Risk:** Extracted findings could be meaningless or misleading (e.g., "Found:
Code executed successfully" for trivial operations).

**Mitigation:** Extractors have strict guards — only return a discovery when
the result is meaningful. The `execute_code` extractor only fires on explicit
success status. The `search_files` extractor only fires on non-zero match
count. LLM findings are deduplicated by content hash. If quality is still
an issue, add a minimum length filter (>20 chars) and skip trivial patterns.

### 8.5 Backward Compatibility

**Risk:** Existing agent sessions in progress when code is deployed.

**Mitigation:** All changes are additive — new discovery emissions alongside
existing activities. No change to activity schema or API calls. Running
sessions that don't use `DiscoveryTracker` continue with the old behavior.
A restart picks up the new code.

### 8.6 LLM Finding Extraction Reliability

**Risk:** Regex-based finding extraction from LLM output may produce false
positives (picking up "I found that..." in a hypothetical discussion, not an
actual discovery).

**Mitigation:** The extraction only runs on the *accumulated output* — text
the LLM has already committed to, not stream mid-thought. False positives
produce minor noise (a discovery that isn't really a discovery), which the
user can scroll past. The rate limiter prevents explosion.

---

## 9. Fallback Behavior

| Condition | Behavior |
|-----------|----------|
| Linear API rate-limited (429) | Activity dropped silently; work continues |
| Activity creation fails | Logged at DEBUG; no retry; work continues |
| Discovery extractor raises exception | Logged; treated as "no discovery"; work continues |
| LLM finding extraction empty | Nothing emitted; keepalive continues normally |
| No tool results to extract from | Nothing emitted; next tool call or keepalive fills gap |
| Keepalive context empty | Falls back to "Still working on it..." |
| Tracker not initialized | Falls back to current behavior (keepalive only) |

The invariant: **activity emission is always optional.** The core task
processing (LLM calls, tool execution, response generation) never blocks
on activity delivery.

---

## 10. Documentation

### 10.1 Updated Module Docstring (linear_agent.py)

```
Architecture
────────────
Linear Webhook POST → HMAC verify → IP allowlist → Event router
  → Background asyncio Task
    → Acknowledge (thought activity within 10s — required by Linear)
    → Parse promptContext (issue, comments, guidance)
    → Initialize DiscoveryTracker (emits findings as they happen)
    → Process task (analyze, research, code)
      → Emit discovery activities (Found, Identified, Decided, Created)
      → Derive discoveries from completed tool call results
      → Extract finding statements from LLM output stream
      → Background keepalive with current investigation context
    → Emit response activity with result + work summary
    → Update issue (comment, status, assignee)
```

### 10.2 DiscoveryTracker Docstring

```python
class DiscoveryTracker:
    """Emits discovery activities to show progress during issue work.

    Uses Linear's activity system to reveal findings as they happen.
    Unlike a phase tracker (which announces what the agent is about to
    do), this emits what the agent has found or accomplished.

    Four discovery kinds are supported:
    - Found: something located during investigation
    - Identified: a gap, pattern, or relationship recognized
    - Decided: a choice made between alternatives
    - Created: an artifact produced

    Key behaviors:
    - Each non-keepalive activity carries information the user didn't
      have before.
    - Tool call results are mined for discoveries via extractors.
    - LLM streaming output is scanned for finding statements every 10s.
    - All emissions are fire-and-forget — never block the main work.
    - Rate-limited to MIN_ACTIVITY_INTERVAL between emissions.
    """
```

### 10.3 Configuration Reference

```yaml
# ~/linear-agent/.env
# Activity emission tuning:

# How often the keepalive fires during LLM processing (seconds)
# KEEPALIVE_INTERVAL_S=15

# Minimum interval between activity emissions (seconds)
# MIN_ACTIVITY_INTERVAL_S=1.5

# Minimum interval between persistent discoveries (seconds)
# MILESTONE_INTERVAL_S=3.0

# Progress verbosity: "full" (default), "sparse" (discoveries only, no keepalive context)
# PROGRESS_VERBOSITY=full
```

### 10.4 User-Facing Guide

Create `~/linear-agent/docs/progress-visibility.md`:

```markdown
# Progress Visibility

When Hermes works your issue, it reveals findings as they happen in the
Linear activity timeline. Instead of seeing raw tool calls and "Still
thinking..." messages, you see a trail of discoveries:

1. **Found** — Files located, patterns matched, search results
2. **Identified** — Gaps spotted, root causes recognized
3. **Decided** — Approaches chosen, alternatives evaluated
4. **Created** — Plans written, code changed, documents produced

Each activity carries information you didn't have before. Scrolling back
through the timeline tells the story of how the answer was assembled.

## What stays hidden
- Raw chain-of-thought reasoning
- Failed attempts and retries
- Credentials and internal prompts
- Individual tool call JSON

## Configuration

Set `PROGRESS_VERBOSITY=sparse` in the Linear agent environment to see
only major discoveries (no in-progress indicators or thought snippets).
```

---

## 11. Implementation Order

| Step | Description | Estimated Effort |
|------|-------------|-----------------|
| 1 | Add `DiscoveryTracker` class with discovery helper methods and rate limiting | 1-2 hours |
| 2 | Integrate `DiscoveryTracker` into `_handle_analysis()` — initialize, wire discovery emission at key points | 1 hour |
| 3 | Add tool result → discovery mapping (`_DISCOVERY_EXTRACTORS` dict) | 1 hour |
| 4 | Add LLM finding extraction from streaming output (replace content-tail snippets) | 1 hour |
| 5 | Enhance keepalive with discovery context (replace rotating message list) | 30 min |
| 6 | Wire discovery emissions in `_handle_analysis()` — emit at investigation boundaries | 30 min |
| 7 | Add fallback handling for missing/empty discoveries | 15 min |
| 8 | Test with real issue workflows | 1 hour |
| 9 | Write documentation (docstring, config, user-facing guide) | 30 min |
| **Total** | | **~6-8 hours** |

---

## 12. Success Criteria

The implementation is complete when:

1. **Every non-keepalive activity carries new information** — Scrolling the
   activity timeline shows a trail of findings (Found, Identified, Decided,
   Created), not tool calls or status badges.

2. **No gap longer than 10s** without visible activity during active
   processing. Keepalive includes current investigation context.

3. **Tool results produce discoveries** — After meaningful tool calls
   (search_files, read_file, web_search), a discovery activity appears with
   what was found, not just what tool ran.

4. **LLM findings extracted** — During long generation, the 10s thought
   snippet contains a meaningful finding statement, not the last 60 chars of
   raw output.

5. **Rate safety** — >2,400 req/h headroom on Linear API rate limit.

6. **Fallback clean** — API errors during activity emission never crash or
   delay the actual task processing.

7. **No regression** — Contextual keepalive still fires if no discovery has
   been emitted within 15s.

---

*This plan is a proposal for review. Once approved, implementation can begin.*
