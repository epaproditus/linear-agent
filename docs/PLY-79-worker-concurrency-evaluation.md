# PLY-79: Hermes Worker Concurrency and Multi-Issue Behavior

**Date:** 2026-06-29  
**Scope:** `linear-agent` (FastAPI on port 8660) and its interaction with the Hermes API server (port 8642).

---

## Executive Summary

Hermes does **not** process multiple issues strictly sequentially. The linear-agent service uses **asyncio cooperative concurrency** in a **single uvicorn process** with a **10-slot semaphore** (`MAX_CONCURRENT_SESSIONS`). Up to 10 distinct issue sessions can run in parallel; an 11th waits until a slot frees. Work is **not** rejected when over capacity — tasks queue indefinitely on the semaphore.

The practical throughput ceiling is dominated by the **Hermes API server** (long-running agent loops with server-side tools), not by linear-agent's webhook handler. linear-agent itself is designed for modest parallelism (10 concurrent sessions) with several ingress and emission rate limits.

**Verdict:** Concurrency exists but is **bounded and uneven** across code paths. For the expected "tag Hermes on several issues at once" workload, behavior is **parallel up to 10 issues**, then **FIFO queueing with no backpressure signal to Linear**.

---

## Architecture (Current)

```
Linear webhook (×N)
  → FastAPI handler (single event loop, single uvicorn worker)
    → SlidingWindowRateLimiter (30 req / 60s → HTTP 429)
    → Event router (dedup, team allowlist, self-loop guard)
    → asyncio.create_task(background work)
      → asyncio.Semaphore(10)
        → TaskProcessor.process()
          → Hermes plan call (blocking HTTP)
          → Hermes SSE stream (600s timeout, tools server-side)
          → Hermes finalize call (blocking HTTP)
          → Linear GraphQL (response, status, delegate)
```

Deployment (`linear-agent-user.service`):

```ini
ExecStart=... uvicorn linear_agent:app --host 0.0.0.0 --port 8660
```

No `--workers` flag → **one process, one event loop**.

---

## Measured Concurrency Behavior

Automated tests in `tests/test_concurrency.py` validate the following (run with `pytest tests/test_concurrency.py -v`):

| Scenario | Observed behavior |
|----------|-------------------|
| 5 distinct `AgentSessionEvent` webhooks fired together | Sessions **overlap** in time (`max_active > 1`); wall-clock ≈ one session duration, not 5× |
| 13 concurrent sessions (cap = 10) | `max_active` peaks at **10**; extras wait on semaphore |
| Duplicate webhook for same `session_id` while running | Returns `"already running"` (no second task) |
| Duplicate webhook within 60s dedup TTL | Returns `"deduped"` |
| `@mention` comment path | Spawns `create_task(_process_with_semaphore)` — **uses semaphore, not `_active_runs`** |
| Stop signal on active session | Cancels task, removes from `_active_runs` |

**Conclusion:** Multi-issue tagging is **concurrent, not sequential**, up to the configured cap.

---

## Concurrency Limits (Complete Inventory)

| Layer | Limit | Mechanism | On exceed |
|-------|-------|-----------|-----------|
| Webhook ingress | 30 requests / 60s | `SlidingWindowRateLimiter` | HTTP 429 |
| Active LLM sessions | 10 | `asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)` | Task **waits** (unbounded queue) |
| Per-session duplicate | 1 | `_active_runs` dict (`AgentSessionEvent` only) | `"already running"` |
| Webhook dedup | 60s TTL | `_dedup_cache` | `"deduped"` |
| Activity emission | 0.8s / 1.5s intervals | `DiscoveryTracker._emit` | Drops excess |
| Tool progress POSTs | 1.5s between POSTs | `ProgressQueueWorker` | Skips items faster than interval |
| Hermes SSE keepalive | 5s drought injection | `_call_llm` loop | Injects synthetic progress |
| Session keepalive | 15s | `_keep_session_alive` | Ephemeral thought to Linear |
| Hermes HTTP | 600s stream / 180s finalize | `httpx` timeouts | Retry once (5s backoff) on 5xx/timeout |
| Linear webhook ack | 5s | `WEBHOOK_TIMEOUT_S` | Linear retries webhook |

Constants (`linear_agent.py` lines 121–124):

```python
MAX_CONCURRENT_SESSIONS = 10
RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX_REQUESTS = 30
```

---

## What Happens When Multiple Issues Arrive Close Together

### Scenario A: 3 issues @-mentioned within seconds (AgentSessionEvent path)

1. Each webhook is accepted independently (if under 30/min rate limit).
2. Each spawns `asyncio.create_task(_run_session)`.
3. All three acquire semaphore slots immediately and run **in parallel**.
4. Each opens its own Hermes SSE stream (`X-Hermes-Session-Id` header).
5. Each has its own `ProgressQueueWorker` and `DiscoveryTracker`.
6. Linear timeline updates are rate-limited per session but concurrent across sessions.

### Scenario B: 15 issues tagged in a burst

1. First 10 begin processing immediately.
2. Issues 11–15 **block on the semaphore** — no error, no Linear notification.
3. As each of the first 10 completes, the next queued task starts (FIFO via asyncio).
4. If burst also exceeds 30 webhooks/minute, excess webhooks get **HTTP 429** before spawning tasks.

### Scenario C: Same issue tagged twice while first run is active

1. Second `AgentSessionEvent` returns `"already running"`.
2. User sees no new work started until the first completes.
3. **Exception:** `@mention` and assignment paths do **not** register in `_active_runs`, so a duplicate @mention could spawn a second task for the same issue (semaphore still applies).

### Scenario D: Hermes API under load

Each session runs a full agent loop on the Hermes server (tools, terminal, filesystem). With 10 concurrent streams:

- Hermes server CPU/RAM/disk become the bottleneck (see `reports/ply-17-hermes-latency-report.md`: 12–59s per interaction, host I/O saturation).
- linear-agent shares one `httpx.AsyncClient` for all Linear GraphQL calls.
- Progress emissions to Linear are throttled to protect API headroom.

---

## Bottleneck Analysis

| Rank | Bottleneck | Impact |
|------|------------|--------|
| 1 | **Hermes API server** | Dominates wall-clock; each issue = plan + stream + finalize |
| 2 | **MAX_CONCURRENT_SESSIONS = 10** | Hard cap; no visibility to users when queued |
| 3 | **Sequential Hermes calls per issue** | 3 round-trips (plan → stream → finalize) per session |
| 4 | **Single uvicorn process** | All webhooks + SSE parsing share one event loop |
| 5 | **Linear API emission rate limits** | Tool progress faster than 1.5s is dropped |
| 6 | **Comment/assignment pre-work** | `create_session` + `get_issue` GraphQL before HTTP 200 |
| 7 | **Unbounded semaphore wait queue** | Memory growth under sustained overload |

---

## Code Path Asymmetry (Gap)

| Path | Semaphore | `_active_runs` | Stop signal | Duplicate guard |
|------|-----------|----------------|-------------|---------------|
| `AgentSessionEvent` | Yes | Yes | Yes | Yes |
| `@mention` comment | Yes | **No** | **No** | Dedup only (comment ID) |
| Issue assignment | Yes | **No** | **No** | **No** |

The `@mention` and assignment paths call `asyncio.create_task(self._process_with_semaphore(...))` directly without registering in `_active_runs`. This means stop signals and "already running" protection only work for the native AgentSessionEvent flow.

---

## Is Sequential Processing a Problem?

**Partially.** The "sequential" hypothesis is **incorrect for distinct issues** — they run in parallel up to 10. However:

1. **Effective throughput** is low when Hermes calls take 30–60s each → ~10 issues / minute theoretical max, less under Hermes load.
2. **Queueing is invisible** — issues 11+ appear delegated but Hermes hasn't started yet.
3. **No horizontal scaling** — single uvicorn worker cannot use multiple CPU cores for asyncio tasks.
4. **Hermes is the real worker** — linear-agent is a thin orchestrator; scaling requires Hermes capacity, not just linear-agent config.

---

## Recommendations

### Tier 1 — Low effort, high value (follow-up tickets)

1. **Unify session tracking across all entry paths**  
   Register `@mention` and assignment tasks in `_active_runs` so stop signals and duplicate guards work consistently.

2. **Surface queue position to Linear**  
   When semaphore acquisition waits > N seconds, emit an ephemeral thought: "Queued — N sessions ahead."

3. **Make limits configurable via env**  
   `MAX_CONCURRENT_SESSIONS`, `RATE_LIMIT_MAX_REQUESTS` as `Settings` fields for tuning without code changes.

4. **Add semaphore queue depth metric**  
   Log/metric: `waiting_tasks`, `active_tasks`, `hermes_in_flight` for operational visibility.

### Tier 2 — Moderate effort

5. **Backpressure on overload**  
   When wait queue exceeds a threshold (e.g. 20), reject new sessions with a Linear response: "At capacity — try again shortly." Prefer explicit rejection over silent queueing.

6. **Reduce per-session Hermes round-trips**  
   Evaluate merging plan + main call, or making finalize optional for simple queries. Cuts latency ~30% for short tasks.

7. **Dedicated Linear HTTP connection pool sizing**  
   Configure `httpx.Limits(max_connections=20)` on shared `LinearClient` for 10+ concurrent sessions.

### Tier 3 — Architecture rethink (if >10 concurrent issues is a requirement)

8. **Job queue with worker pool**

```
Linear webhook → enqueue(job) → return 200
Worker pool (N processes) → dequeue → Hermes API → Linear API
```

| Approach | Pros | Cons |
|----------|------|------|
| **Redis + RQ/Celery** | Durable queue, horizontal workers, retry/DLQ | New infra, deployment complexity |
| **Multiple uvicorn workers + Redis dedup** | Simple scale-out | Shared state (`_active_runs`, dedup) needs Redis |
| **Separate "orchestrator" + "executor" services** | Clean separation, independent scaling | Largest refactor |

9. **Hermes-side concurrency policy**  
   Coordinate with Hermes API to enforce per-tenant concurrency limits and fair scheduling across sessions.

10. **Priority queue**  
    Urgent issues (label/priority) dequeue first instead of FIFO.

---

## Suggested Implementation Order

| Step | Work | Effort |
|------|------|--------|
| 1 | Unify `_active_runs` for all paths | Small |
| 2 | Env-configurable concurrency limits | Small |
| 3 | Queue-wait ephemeral notification | Small |
| 4 | Metrics/logging for concurrency state | Small |
| 5 | Backpressure with user-visible message | Medium |
| 6 | Hermes call consolidation (plan/finalize) | Medium |
| 7 | External job queue (if sustained >10 concurrent needed) | Large |

---

## Test Commands

```bash
LINEAR_API_KEY=test LINEAR_WEBHOOK_SECRET=test python3 -m pytest tests/test_concurrency.py -v
```

---

## References

- `linear_agent.py` — `AgentWebhookHandler`, `MAX_CONCURRENT_SESSIONS`, `ProgressQueueWorker`
- `linear-agent-user.service` — single-worker uvicorn deployment
- `reports/ply-17-hermes-latency-report.md` — Hermes host latency analysis
- `docs/progress-visibility.md` — activity emission rate limiting
- `PROJECT_SUMMARY.md` — open item: "Multi-session handling at scale"
