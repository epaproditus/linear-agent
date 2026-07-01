# PLY-79: Concurrency Architecture Recommendations

**Date:** 2026-06-29  
**Builds on:** [`PLY-79-worker-concurrency-evaluation.md`](./PLY-79-worker-concurrency-evaluation.md)  
**Scope:** `linear-agent` (FastAPI, port 8660) + Hermes API server (port 8642)

This document translates public architecture patterns and production guidance into **concrete, prioritized recommendations** for Hermes as a Linear agent. It assumes the baseline evaluation is already done: Hermes runs **up to 10 sessions in parallel** via `asyncio.Semaphore`, then **FIFO-waits** with no user-visible signal.

---

## Current State (Summary)

| Layer | Mechanism | Gap |
|-------|-----------|-----|
| Ingress | `SlidingWindowRateLimiter` (30/60s) | No queue depth visibility |
| Concurrency | `asyncio.Semaphore(10)` | Unbounded waiters; hardcoded |
| Session safety | `_active_runs` dict | Only `AgentSessionEvent` path |
| Progress | `ProgressQueueWorker` (unbounded `Queue`) | No backpressure on tool-progress flood |
| Cancellation | `task.cancel()` on stop signal | No Hermes upstream abort; comment/assignment paths untracked |
| Deployment | Single uvicorn process | No horizontal scale; no durable queue |

---

## Research Synthesis

### 1. Bounded queues as the primary backpressure primitive

Production asyncio systems use **bounded `asyncio.Queue(maxsize=N)`** so producers block (or reject) when consumers fall behind, instead of accumulating unbounded memory. [CodeSignal backpressure lesson](https://codesignal.com/learn/courses/concurrency-async-io/lessons/backpressure-and-retry-strategies) describes this as the simplest effective throttle: when the queue is full, `await q.put()` suspends the producer until space opens.

For FastAPI webhook ingress, the same pattern maps directly: accept the webhook fast, enqueue a job, return 200 — but **reject or shed load** when the queue is full rather than spawning unbounded background tasks. [FastAPI at Scale](https://medium.com/@2nick2patel2/fastapi-at-scale-minus-the-drama-17228940f816) recommends a bounded in-process queue with HTTP 429 on `queue.full()` for p99 stability, and Redis/Rabbit/Kafka when you need cross-pod durability.

**Hermes mapping:** Today, every accepted webhook immediately spawns `asyncio.create_task()`. Tasks 11+ block on the semaphore inside the task, not at ingress. There is no `maxsize` on the implicit wait queue.

### 2. Two-level lane scheduling (session + global)

OpenClaw's lane system is the closest public analogue to a multi-issue LLM agent gateway. It uses **nested queuing**: first a per-session lane (`maxConcurrent=1`) to prevent race conditions on session state, then a global lane (`maxConcurrent=N`) to cap total parallelism. [OpenClaw queue docs](https://docs.openclaw.ai/concepts/queue), [lane implementation deep-dive](https://openclawlab.com/en/docs/deep-dive/framework-focus/lane-queue-state-machine/).

Key design elements worth adopting:

- **Drain pump:** On enqueue or task completion, immediately try to start more work up to the lane cap (event-driven, no polling). [OpenClaw command-queue.ts](https://github.com/openclaw/openclaw/blob/main/src/process/command-queue.ts)
- **Wait warnings:** `onWait(waitMs, queuedAhead)` callback when queue wait exceeds a threshold (default 2s). Surfaces invisible queueing.
- **Queue modes for mid-run input:** `collect` (merge follow-ups), `followup` (queue for next turn), `interrupt` (cancel active, run new). [OpenClaw queue modes](https://docs.openclaw.ai/concepts/queue)
- **Per-lane caps:** `main`, `cron`, `subagent` lanes don't compete — a cron backlog can't starve inbound chat. [LumaDock concurrency guide](https://lumadock.com/tutorials/openclaw-concurrency-retry-control)

**Hermes mapping:** Hermes has global concurrency (`Semaphore(10)`) but no per-session lane. The `AgentSessionEvent` path drops duplicate prompts (`"already running"`), while `@mention`/assignment paths can spawn overlapping work on the same issue. OpenClaw's two-level model would fix this asymmetry cleanly.

### 3. Bulkhead semaphores per downstream dependency

[LLM best practices for FastAPI async I/O](https://llmbestpractices.com/backend/fastapi-async-io) and [semaphore concurrency guides](https://medium.com/@mr.sourav.raj/mastering-asyncio-semaphores-in-python-a-complete-guide-to-concurrency-control-6b4dd940e10e) recommend **separate semaphores per downstream service** rather than one global cap. Async code can easily open more simultaneous connections than a downstream can absorb.

**Hermes mapping:** A single `MAX_CONCURRENT_SESSIONS` semaphore gates Hermes LLM work, Linear GraphQL, and session lifecycle together. Under 10 concurrent sessions, Linear API emissions (throttled at 1.5s per session) can still approach ~400 mutations/hour — well under Linear's 5,000 req/h limit ([Linear rate limiting](https://linear.app/developers/rate-limiting)), but burst patterns from `create_session` + `get_issue` + plan updates per session add headroom risk. Separate bulkheads would let you tune Hermes vs Linear independently.

### 4. Durable job queues for crash recovery and horizontal scale

For work that must survive process restarts, [ARQ (async Redis queue)](https://davidmuraya.com/blog/fastapi-background-tasks-arq-vs-built-in/) is the natural fit for this codebase: native `async def` workers, simpler than Celery, Redis-backed durability. [FastAPI BackgroundTasks vs ARQ vs Celery comparison](https://medium.com/@komalbaparmar007/fastapi-background-tasks-vs-celery-vs-arq-picking-the-right-asynchronous-workhorse-b6e0478ecf4a) positions ARQ as the sweet spot for "API + durable async jobs" without Celery's operational weight.

For higher-throughput ingestion with explicit consumer lag monitoring, [Redis Streams consumer groups](https://redis.antirez.com/fundamental/streams-consumer-patterns.html) provide at-least-once delivery, `XPENDING` as a backpressure signal, and `XAUTOCLAIM` for stuck-worker recovery. [Redis ingest pipeline tutorial](https://redis.io/tutorials/fast-data-ingest-pipeline-with-redis/) and [MVP Factory Streams guide](https://mvpfactory.io/blog/redis-streams-as-your-startup-s-event-bus-consumer-groups-backpressure-in-ktor/) describe the janitor pattern for poison pills → DLQ.

The [longshot reference implementation](https://github.com/AkshatSoni26/longshot) demonstrates the full pattern for SSE + durable work: Redis Streams broker, idempotency locks, typed cancel events, and **SSE disconnect ≠ work cancel** (explicit `DELETE /jobs/{id}` for cancellation).

**Hermes mapping:** A uvicorn restart mid-Hermes-stream loses in-flight work with no recovery. An external queue would let workers survive API process restarts and scale horizontally.

### 5. Agent lifecycle FSM and cooperative cancellation

Production agent systems need an explicit run state machine with legal transitions, not just `task.cancel()`. [Solana Garden cancellation guide](https://solana.garden/guides/llm-agent-cancellation-timeout-lifecycle-explained/) recommends:

- Propagate `AbortSignal` from API → agent loop → each tool wrapper
- Separate **wall-clock**, **per-tool**, and **LLM idle-stream** timeouts
- Persist `cancelling` state so crash recovery can finish cleanup
- Fan-out cancel to subagents in one transaction, not sequentially
- Register **compensation actions** before mutating tools (Saga pattern)

[Metacto state machine guide](https://www.metacto.com/blogs/ai-agent-state-machine-design) distinguishes three independent layers: orchestration pattern (who calls whom), workflow structure (FSM vs prompt loop), and durable execution (Temporal/checkpoints). Hermes currently conflates all three in one asyncio task.

For httpx SSE streams specifically, cancellation must explicitly close the response. [HTTPX async docs](https://www.python-httpx.org/async/) require `response.aclose()` in `finally` blocks; [httpx PR #2148](https://github.com/encode/httpx/pull/2148) fixed connection leaks on `CancelledError` during stream reads. Without this, cancelled sessions can exhaust the connection pool ([httpx issue #1461](https://github.com/encode/httpx/issues/1461)).

**Hermes mapping:** Stop signals cancel the asyncio task but do not abort the Hermes upstream stream or server-side tool execution. `_call_llm()` has no explicit `CancelledError` handler or `aclose()` in a `finally` block.

### 6. Durable workflows for long-running agent runs (Tier 3)

If sessions routinely exceed minutes and must survive infrastructure failure, [Temporal activity heartbeats](https://github.com/temporal-sa/temporal-design-patterns/blob/main/docs/long-running-activity.md) enable fast stuck-worker detection and cancellation delivery. [Temporal timeout types](https://temporal.io/blog/activity-timeouts) recommend short `HeartbeatTimeout` (~30s) with long `StartToCloseTimeout` for LLM work. [xgrid.ai Temporal AI pitfalls](https://www.xgrid.co/resources/temporal-ai-agent-orchestration-failure-patterns/) warns that missing heartbeats cause duplicate LLM calls on retry.

[GMI Cloud orchestration guide](https://www.gmicloud.ai/en/blog/ai-agent-workflow-orchestration-production-2026) and [Kunwar orchestration chapter](https://www.kunwar.page/chapter/072-designing-an-agent-orchestration-layer) position Temporal as the right choice when you need crash-resumable multi-step agents with automatic retries, DLQ, and billing per session — at the cost of new infrastructure.

**Hermes mapping:** Overkill for current scale (≤10 concurrent, single host), but the right long-term architecture if Hermes becomes a multi-tenant agent platform.

### 7. Linear API as a shared budget

Linear enforces a **leaky bucket** on both request count (5,000/hr OAuth) and query complexity (250,000 pts/hr) ([Linear rate limiting](https://linear.app/developers/rate-limiting)). `agentActivityCreate` mutations count toward this budget. Hermes already rate-limits emissions (1.5s persistent, 0.8s any) in `DiscoveryTracker`, which is correct. Under 10 concurrent sessions, theoretical max is ~24 persistent activities/minute per session × 10 = 240/min — but throttling drops excess, which is the right tradeoff ([progress-visibility.md](./progress-visibility.md)).

Recommendation: **read `X-RateLimit-Requests-Remaining` response headers** and dynamically widen emission intervals when headroom is low ([Linear rate limit skill patterns](https://github.com/jeremylongshore/claude-code-plugins-plus-skills/blob/main/plugins/saas-packs/linear-pack/skills/linear-rate-limits/SKILL.md)).

---

## Prioritized Recommendations

Recommendations are ordered by **impact ÷ effort**. Each includes where to change code, what to implement, and tradeoffs.

### P0 — Quick wins (hours, high impact)

#### P0.1 Unify session tracking across all entry paths

**Problem:** `@mention` and assignment paths use `_process_with_semaphore()` without `_active_runs` registration. Stop signals and "already running" guards only work on `AgentSessionEvent`.

**Implementation:**
```python
# In _handle_comment and _handle_issue_update, replace:
asyncio.create_task(self._process_with_semaphore(session, issue))
# With:
task = asyncio.create_task(self._run_session(session))  # reuse _run_session
self._active_runs[session.session_id] = task
```

**Files:** `linear_agent.py` — `_handle_comment`, `_handle_issue_update`, `_run_session` (accept optional pre-fetched `issue`).

**Sources:** [OpenClaw per-session lane serialization](https://docs.openclaw.ai/concepts/queue)

---

#### P0.2 Env-configurable concurrency limits

**Problem:** `MAX_CONCURRENT_SESSIONS = 10` is hardcoded. Tuning requires a code deploy.

**Implementation:** Add to `Settings`:
```python
max_concurrent_sessions: int = 10
rate_limit_max_requests: int = 30
rate_limit_window_s: float = 60.0
```
Wire into `AgentWebhookHandler.__init__` and document in `.env.example`.

**Sources:** [FastAPI semaphore per dependency](https://llmbestpractices.com/backend/fastapi-async-io)

---

#### P0.3 Cooperative SSE cancellation in `_call_llm`

**Problem:** `task.cancel()` may leave httpx streams open, leaking connections and leaving Hermes server-side tools running.

**Implementation:**
```python
async def _call_llm(self, ...) -> str:
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream(...) as resp:
                try:
                    async for line in resp.aiter_lines():
                        ...
                finally:
                    await resp.aclose()
    except asyncio.CancelledError:
        # Optionally: POST /chat/completions/{session_id}/cancel to Hermes
        log.info("LLM stream cancelled for session %s", session_id[:8])
        raise
```

Add `X-Hermes-Cancel-Token` or call a Hermes cancel endpoint if one exists.

**Sources:** [HTTPX async streaming](https://www.python-httpx.org/async/), [httpx cancellation fix](https://github.com/encode/httpx/pull/2148), [cooperative cancellation FSM](https://solana.garden/guides/llm-agent-cancellation-timeout-lifecycle-explained/)

---

#### P0.4 Queue-wait visibility to Linear

**Problem:** Issues 11+ are delegated but Hermes hasn't started — users see silence.

**Implementation:** Wrap semaphore acquisition:
```python
async def _acquire_with_notice(self, session_id: str, tracker: DiscoveryTracker | None):
    if self._concurrency_semaphore.locked():
        # Approximate waiters: tasks waiting = locked slots not yet acquired
        waiters = max(0, MAX_CONCURRENT - self._concurrency_semaphore._value)
        if tracker and waiters > 0:
            await tracker.in_progress(f"Queued — ~{waiters} sessions ahead")
    await self._concurrency_semaphore.acquire()
```

OpenClaw's `onWait(waitMs, queuedAhead)` callback is the reference pattern. Emit at most once per session.

**Sources:** [OpenClaw wait warnings](https://github.com/openclaw/openclaw/blob/main/src/process/command-queue.ts), [OpenClaw queue docs](https://docs.openclaw.ai/concepts/queue)

---

### P1 — Medium effort (days, high impact)

#### P1.1 Bounded `SessionQueue` with explicit backpressure

**Problem:** Unbounded semaphore waiters and unbounded `ProgressQueueWorker` queue risk memory growth under burst.

**Implementation:** Introduce a `SessionQueue` class:
```python
class SessionQueue:
    def __init__(self, max_waiting: int = 20, max_concurrent: int = 10):
        self._waiting: asyncio.Queue[AgentSession] = asyncio.Queue(maxsize=max_waiting)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._dispatcher_task: asyncio.Task | None = None

    async def enqueue(self, session: AgentSession) -> str:
        if self._waiting.full():
            return "rejected"  # caller sends Linear response
        await self._waiting.put(session)
        return "queued"

    async def _dispatch(self):
        while True:
            session = await self._waiting.get()
            await self._semaphore.acquire()
            asyncio.create_task(self._run_and_release(session))
```

On `"rejected"`, respond via Linear: *"At capacity — please try again in a few minutes."*

**Tradeoff:** Explicit rejection vs silent queueing. Production systems prefer fast rejection for p99 stability ([FastAPI at Scale](https://medium.com/@2nick2patel2/fastapi-at-scale-minus-the-drama-17228940f816)).

**Sources:** [Bounded queue backpressure](https://codesignal.com/learn/courses/concurrency-async-io/lessons/backpressure-and-retry-strategies), [producer-consumer queues](https://medium.com/@caring_smitten_gerbil_914/mastering-asynchronous-queues-in-python-concurrency-made-easy-with-asyncio-878566ef9d7d)

---

#### P1.2 Two-level lane scheduler (session + global)

**Problem:** Same issue can get overlapping tasks from different entry paths; no queue mode for follow-up prompts during an active run.

**Implementation:** Port OpenClaw's lane model (simplified):
```python
class LaneScheduler:
    """Per-session serial lane + global concurrent lane."""
    def __init__(self, global_max: int = 10):
        self._session_lanes: dict[str, asyncio.Lock] = {}
        self._global_sem = asyncio.Semaphore(global_max)

    async def run(self, session_id: str, coro):
        lock = self._session_lanes.setdefault(session_id, asyncio.Lock())
        async with lock:          # session lane: serial
            async with self._global_sem:  # global lane: parallel across sessions
                return await coro
```

For follow-up prompts while running, add queue mode config:
- `collect` — merge queued prompts into one follow-up turn (default)
- `followup` — process after current run ends
- `interrupt` — cancel active, start new (maps to existing stop signal)

**Files:** New `lane_scheduler.py`; refactor `AgentWebhookHandler` to route all paths through `LaneScheduler.run()`.

**Sources:** [OpenClaw lanes](https://docs.openclaw.ai/concepts/queue), [lane state machine](https://openclawlab.com/en/docs/deep-dive/framework-focus/lane-queue-state-machine/), [LumaDock guide](https://lumadock.com/tutorials/openclaw-concurrency-retry-control)

---

#### P1.3 Separate bulkhead semaphores

**Problem:** One semaphore couples Hermes capacity to Linear API capacity.

**Implementation:**
```python
self._hermes_sem = asyncio.Semaphore(settings.max_concurrent_sessions)  # 10
self._linear_sem = asyncio.Semaphore(settings.max_linear_concurrent)    # 15
```

Wrap `_call_llm*` with `_hermes_sem`, wrap `LinearClient._gql` with `_linear_sem`. Tune independently based on observed bottlenecks.

**Sources:** [Bulkhead pattern](https://medium.com/@2nick2patel2/fastapi-at-scale-minus-the-drama-17228940f816), [per-dependency semaphores](https://llmbestpractices.com/backend/fastapi-async-io)

---

#### P1.4 Linear API adaptive rate limiting

**Problem:** Fixed 1.5s emission interval doesn't respond to Linear `RATELIMITED` errors.

**Implementation:** In `LinearClient._gql`, parse response headers and `RATELIMITED` errors:
```python
remaining = int(resp.headers.get("X-RateLimit-Requests-Remaining", 9999))
if remaining < 500:
    self._emit_interval_multiplier = 2.0  # DiscoveryTracker reads this
```

On `RATELIMITED`, exponential backoff with jitter before retry ([Linear rate limiting](https://linear.app/developers/rate-limiting)).

**Sources:** [Linear rate limits](https://linear.app/developers/rate-limiting), [rate limit skill](https://github.com/jeremylongshore/claude-code-plugins-plus-skills/blob/main/plugins/saas-packs/linear-pack/skills/linear-rate-limits/SKILL.md)

---

#### P1.5 Bounded `ProgressQueueWorker` with drop-oldest

**Problem:** `asyncio.Queue()` has no `maxsize`. A flood of `hermes.tool.progress` events during heavy tool use can grow memory unboundedly.

**Implementation:**
```python
self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=50)

async def put(self, text: str) -> None:
    if self._queue.full():
        try:
            self._queue.get_nowait()  # drop oldest
            self._queue.task_done()
        except asyncio.QueueEmpty:
            pass
    await self._queue.put(text)
```

Aligns with OpenClaw's `drop: summarize` queue mode — keep the latest progress, not every intermediate event.

**Sources:** [OpenClaw queue cap/drop](https://docs.openclaw.ai/concepts/queue), [bounded queue backpressure](https://codesignal.com/learn/courses/concurrency-async-io/lessons/backpressure-and-retry-strategies)

---

#### P1.6 Observability: concurrency gauges

**Problem:** No metrics for `active_sessions`, `waiting_sessions`, `hermes_streams_open`.

**Implementation:** Log structured events at state transitions; expose `/health/concurrency` endpoint:
```json
{
  "active": 7,
  "waiting": 3,
  "semaphore_available": 3,
  "rate_limit_window_count": 12,
  "oldest_waiter_age_s": 45.2
}
```

Scale workers based on **queue depth**, not CPU ([FastAPI at Scale](https://medium.com/@2nick2patel2/fastapi-at-scale-minus-the-drama-17228940f816)).

---

### P2 — Larger investments (weeks, strategic)

#### P2.1 ARQ + Redis durable job queue

**When:** Sustained need for >10 concurrent sessions, crash recovery, or horizontal API scaling.

**Architecture:**
```
Linear webhook → validate HMAC → enqueue ARQ job → HTTP 200
ARQ worker(s) → LaneScheduler → Hermes SSE → Linear API
Redis → job status, dedup keys, _active_runs (shared state)
```

**Implementation sketch:**
```python
# tasks.py
async def process_agent_session(ctx, session_payload: dict):
    handler = AgentWebhookHandler.from_settings()
    await handler.processor.process(AgentSession(**session_payload), ...)

class WorkerSettings:
    functions = [process_agent_session]
    max_jobs = 10
    job_timeout = 900
    redis_settings = RedisSettings.from_dsn(os.environ["REDIS_URL"])
```

Use `_job_id=f"session:{session_id}"` for idempotent enqueue ([ARQ dedup](https://www.stacklesson.com/react-fastapi/fastapi-uploads-tasks/ch30-lesson-04-task-queues-with-arq/)).

**Tradeoffs:**

| | In-process (today) | ARQ + Redis |
|--|--|--|
| Crash recovery | None | Jobs survive restart |
| Horizontal scale | No | Add worker containers |
| Latency | Lowest | +Redis round-trip (~1ms) |
| Ops complexity | Minimal | Redis deploy + monitor |

**Sources:** [ARQ vs BackgroundTasks](https://davidmuraya.com/blog/fastapi-background-tasks-arq-vs-built-in/), [ARQ FastAPI integration](https://medium.com/@a-jns/arq-as-fastapi-background-tasks-alternative-b699cd31cbcb), [fastapi-arq template](https://github.com/davidmuraya/fastapi-arq)

---

#### P2.2 Redis Streams for webhook ingestion (alternative to ARQ)

**When:** Webhook burst rate exceeds processing capacity and you need consumer lag monitoring.

**Pattern:**
1. Webhook handler: `XADD hermes:jobs MAXLEN ~ 1000 * payload <json>`
2. Workers: `XREADGROUP GROUP workers consumer1 COUNT 1 BLOCK 5000 STREAMS hermes:jobs >`
3. Janitor: `XAUTOCLAIM` for stuck messages → DLQ after 3 retries
4. Monitor: `XINFO GROUPS` lag as backpressure signal

**Sources:** [Redis Streams consumer patterns](https://redis.antirez.com/fundamental/streams-consumer-patterns.html), [Redis ingest tutorial](https://redis.io/tutorials/fast-data-ingest-pipeline-with-redis/), [Streams backpressure](https://mvpfactory.io/blog/redis-streams-as-your-startup-s-event-bus-consumer-groups-backpressure-in-ktor/)

---

#### P2.3 Reduce per-session Hermes round-trips

**Problem:** Each issue = plan (60s timeout) + stream (600s) + finalize (180s). Three serial round-trips dominate latency.

**Options:**
1. **Skip plan for simple prompts** — heuristics on body length / keywords
2. **Merge finalize into stream** — system prompt instructs "conclusions only" in the final tokens
3. **Parallel plan + context fetch** — `asyncio.gather(plan_task, get_issue_task)`

**Impact:** ~30% latency reduction for short tasks (per evaluation bottleneck analysis).

---

#### P2.4 Hermes-side concurrency policy

Coordinate with Hermes API to add:
- Per-tenant `max_concurrent_streams`
- Fair scheduling (round-robin across sessions, not FIFO within one tenant)
- `DELETE /sessions/{id}` cancel endpoint that aborts server-side tools
- Stream idle timeout (no tokens for N seconds → auto-cancel)

**Sources:** [GMI Cloud orchestration](https://www.gmicloud.ai/en/blog/ai-agent-workflow-orchestration-production-2026), [idle stream timeout](https://solana.garden/guides/llm-agent-cancellation-timeout-lifecycle-explained/)

---

#### P2.5 Temporal durable workflows (long-term)

**When:** Multi-step agents must survive worker crashes, need automatic retry/DLQ, or billing per session.

Model each agent session as a Temporal workflow with activities for `plan`, `stream`, `finalize`, and `linear_emit`. Use 30s heartbeat timeout on the stream activity.

**Tradeoff:** Significant infrastructure (Temporal cluster) and refactor. Justified only if Hermes becomes a multi-tenant platform with SLA requirements.

**Sources:** [Temporal long-running activities](https://github.com/temporal-sa/temporal-design-patterns/blob/main/docs/long-running-activity.md), [Temporal AI pitfalls](https://www.xgrid.co/resources/temporal-ai-agent-orchestration-failure-patterns/), [Kunwar orchestration chapter](https://www.kunwar.page/chapter/072-designing-an-agent-orchestration-layer)

---

## Recommended Implementation Roadmap

```
Phase 1 (P0, ~1-2 days)
├── P0.1 Unify _active_runs across paths
├── P0.2 Env-configurable limits
├── P0.3 SSE cancellation + aclose()
└── P0.4 Queue-wait Linear notification

Phase 2 (P1, ~1 week)
├── P1.1 Bounded SessionQueue + reject-at-capacity
├── P1.2 LaneScheduler (session + global)
├── P1.5 Bounded ProgressQueueWorker
├── P1.6 /health/concurrency metrics
└── P1.4 Linear adaptive rate limiting

Phase 3 (P2, as needed)
├── P2.1 ARQ + Redis (if >10 concurrent or HA required)
├── P2.3 Hermes round-trip consolidation
└── P2.4 Hermes cancel API + fair scheduling
```

---

## Decision Matrix: When to Escalate Architecture

| Symptom | Stay in-process (P0/P1) | Move to ARQ (P2.1) | Move to Temporal (P2.5) |
|---------|--------------------------|--------------------|-----------------------|
| ≤10 concurrent issues | ✅ | Overkill | Overkill |
| Burst 20-50 issues | P1.1 reject + queue | ✅ | Overkill |
| API restart loses work | P0.3 cancellation | ✅ | ✅ |
| Multi-host deployment | ❌ (shared state) | ✅ | ✅ |
| Session >30 min, must survive crash | ❌ | Partial | ✅ |
| Billing/audit per step | ❌ | Partial | ✅ |

---

## References

### Codebase
- [`linear_agent.py`](../linear_agent.py) — `MAX_CONCURRENT_SESSIONS`, `AgentWebhookHandler`, `ProgressQueueWorker`
- [`docs/progress-visibility.md`](./progress-visibility.md) — activity emission rate limits
- [`docs/PLY-79-worker-concurrency-evaluation.md`](./PLY-79-worker-concurrency-evaluation.md) — baseline evaluation
- [`tests/test_concurrency.py`](../tests/test_concurrency.py) — automated concurrency tests

### External
- [OpenClaw command queue](https://docs.openclaw.ai/concepts/queue) — two-level lane scheduling
- [FastAPI at Scale](https://medium.com/@2nick2patel2/fastapi-at-scale-minus-the-drama-17228940f816) — bounded queues, bulkheads
- [FastAPI async I/O best practices](https://llmbestpractices.com/backend/fastapi-async-io) — semaphores, `to_thread`
- [CodeSignal backpressure](https://codesignal.com/learn/courses/concurrency-async-io/lessons/backpressure-and-retry-strategies) — bounded `asyncio.Queue`
- [ARQ + FastAPI](https://davidmuraya.com/blog/fastapi-background-tasks-arq-vs-built-in/) — durable async job queue
- [Redis Streams consumer patterns](https://redis.antirez.com/fundamental/streams-consumer-patterns.html) — at-least-once, PEL, XAUTOCLAIM
- [longshot SSE + Redis Streams](https://github.com/AkshatSoni26/longshot) — reference implementation
- [LLM agent cancellation lifecycle](https://solana.garden/guides/llm-agent-cancellation-timeout-lifecycle-explained/) — FSM, AbortSignal, compensation
- [Temporal activity heartbeats](https://github.com/temporal-sa/temporal-design-patterns/blob/main/docs/long-running-activity.md) — long-running agent durability
- [Linear rate limiting](https://linear.app/developers/rate-limiting) — API budget constraints
- [HTTPX async cancellation](https://www.python-httpx.org/async/) — stream cleanup on cancel
