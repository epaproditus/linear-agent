# PLY-165 — Warp cloud agents capability map for Hermes Linear agent

**Status:** investigation complete (2026-07-05)  
**Audience:** product, operators, future implementation work  
**Sources:** [Warp cloud agents overview](https://docs.warp.dev/platform/), [Oz API reference](https://docs.warp.dev/reference/api-and-sdk), [Environments](https://docs.warp.dev/platform/environments), [Cloud agents](https://docs.warp.dev/platform/agents/), Hermes `linear-agent` codebase and architecture docs

---

## Executive summary

Warp’s Oz platform is a **general-purpose agent orchestration control plane**: triggers → tracked tasks → optional isolated environments → agent execution → persistent, shareable run records → programmatic APIs. Hermes as Linear agent is a **thin integration adapter** around one long-lived Hermes session per Linear `AgentSession`, with strong issue-context injection and live timeline progress — but **no first-class run registry, no cross-trigger scheduling, and no reproducible execution environments**.

The biggest gaps are not “can the agent think?” but **platform primitives**: durable run identity, trigger breadth, environment isolation, team-wide run catalog, and APIs for monitoring. The highest-leverage first slice is a **Hermes `/v1/runs` lifecycle + Linear run correlation** layer — already on the backlog — because it unlocks observability, debugging, and future triggers without rewriting the thin-adapter model.

---

## Capability map

Legend: **Existing** = shipped today in Hermes Linear agent · **Partial** = some surface exists but incomplete · **Missing** = not implemented · **Later** = valuable but not minimal first slice

| Capability area | Warp (Oz) | Hermes Linear agent today | Gap severity | Table stakes vs differentiator |
|-----------------|-----------|---------------------------|--------------|--------------------------------|
| **Triggered execution** | Schedules, Slack, Linear, GitHub Actions, CLI, API, custom webhooks | Linear webhooks only: `AgentSessionEvent` (created/prompted), `Comment` @mention, `Issue` assign/delegate, stop signal | **High** | Table stakes for “platform”; Linear-only is fine for v1 product wedge |
| **Inspectable run records** | Unified Runs page: prompt, plan, commands, logs, outputs, follow-ups, session link | Linear timeline activities (`thought`/`action`/`response`), Plan UI from Hermes `todo`, PR links on session; Hermes session export manual | **High** | Table stakes for trust/debugging at team scale |
| **Execution context / environments** | Docker image, repo clone, setup commands, clean teardown per run | Single host: Hermes API + `AGENT_WORKDIR`; no per-run isolation | **Medium–High** | Table stakes for multi-repo / CI parity; differentiator = self-hosted homelab simplicity today |
| **Team observability & sharing** | Oz web app, Warp Agent Management Panel, session sharing, filters by source/status/creator | Linear issue thread (team-visible per Linear ACLs); no cross-issue run catalog | **Medium** | Table stakes for ops; Linear-as-UI is a valid MVP |
| **Centralized config** | MCP servers, rules, skills, secrets, env vars scoped per cloud agent identity | `.env` on adapter host; Hermes owns skills/SOUL; legacy mode fetches `/skills` | **Medium** | Table stakes for multi-integration; homelab `.env` OK short-term |
| **Programmatic monitoring APIs** | `POST /agent/run`, `GET /agent/runs`, follow-up/cancel | `GET /health` only on adapter; Hermes sessions/todos via internal API | **High** | Table stakes for automation; implementation detail behind Hermes API |
| **Parallelism & orchestration** | Multi-agent DAG/supervisor, fan-out, cloud + local coordination | `MAX_CONCURRENT_SESSIONS=10`, per-session `asyncio.Task`; Hermes internal tool loop | **Medium** | Differentiator for Warp; optional for Linear agent v1 |
| **Deployment & network boundary** | Warp-hosted, self-hosted workers, managed/unmanaged K8s/CI | FastAPI :8660 + Cloudflare Tunnel, systemd user unit, co-located Hermes :8642 | **Low–Medium** | Differentiator (self-host) vs gap (no worker pool) |

---

## What we already support today

### Triggers (Linear-native)

| Trigger | Behavior | Code / doc |
|---------|----------|------------|
| @-mention in comment | Creates `agentSessionCreateOnIssue`, processes as `created` | `AgentWebhookHandler` |
| Delegate / assign issue | Same session creation path | `Issue` update webhook |
| Follow-up in agent thread | `prompted` with delta context + conversation watermarks | `build_native_turn_message`, `ConversationWatermarkStore` |
| Stop | `agentActivity.signal` → cancel in-flight task | `_active_runs` |
| Team allowlist | `LINEAR_TEAM_IDS` | `Settings` |
| Blocker deferral | Skip work on `created` when blocked | `should_defer_for_blockers` |

### Live execution & UX (strong vs Warp)

- **Streaming tool progress** on Linear timeline via Hermes `hermes.tool.progress` SSE → `DiscoveryTracker` / `ProgressQueueWorker` (curated natural language, not raw tool dumps).
- **Session plans** via Hermes native `todo` → `GET /api/todos/{session_id}` → Linear Agent Plans API.
- **Two-phase response** on first turn: investigate (streamed progress) then optional finalize rewrite (conclusions only).
- **Rich issue context** on created turns: project, siblings, guidance, relations, comments — see `docs/PLY-78-project-context-injection.md`.
- **Workflow nudges**: In Progress on start, In Review after first turn (with gate/blocked exceptions).
- **PR linking** for Linear Diffs UI (`addedExternalUrls`).
- **Security**: HMAC webhook verify, IP allowlist, dedup, self-loop prevention, rate limits.

### Architecture (intentional thin adapter)

Hermes owns memory, tools, todos, skills. Linear adapter owns webhooks, context fetch, timeline mapping, PR links, workflow states. See `docs/linear-agent-architecture-and-learnings.md` and `docs/hermes-native-mode.md`.

**Production requirement:** `HERMES_NATIVE_MODE=1`.

### Adjacent Hermes platform (outside this repo, relevant to gap analysis)

Hermes itself supports gateways (Slack, etc.), cron, webhooks, dashboard, sessions API, secrets — per gateway fixtures and upstream docs. The Linear agent does **not** yet unify these behind a single “run” abstraction.

---

## Biggest capability gaps (ranked)

### 1. Durable, queryable run records (platform primitive)

**Warp:** Every trigger produces a `run_id`, state machine (`QUEUED` → `INPROGRESS` → `SUCCEEDED`/`FAILED`), session link, filterable catalog.

**Hermes today:** Run state is split across:
- Linear activities (human-facing, not machine-queryable)
- Hermes session JSON (manual export)
- In-process `_active_runs` (lost on restart)
- stdout / `journalctl` logs

**Why it matters:** Debugging PLY-112-style sessions, cost attribution, “what ran when?”, and building dashboards all need a stable run ID and lifecycle events.

**Already identified:** `/v1/runs` migration in architecture backlog.

### 2. Trigger breadth

**Missing:** schedules, GitHub Actions step, generic API-triggered runs, Slack-as-trigger (Hermes gateway exists separately), CI failure webhooks.

**Note:** For Linear-first product, this is **expansion**, not blocker — but it defines “agent platform” vs “Linear integration.”

### 3. Reproducible execution environments

**Warp:** Per-run Docker container, pinned image, repo clone, setup commands, teardown.

**Hermes today:** Persistent host filesystem (`AGENT_WORKDIR`), shared Hermes process, no image pinning.

**Risk:** “Works on my agent host” drift; cross-repo tasks depend on pre-provisioned checkouts and SSH rules in prompts.

### 4. Programmatic task visibility API

**Warp:** REST + SDK for create/list/get/cancel/follow-up.

**Hermes today:** `GET /health` on adapter; no run list/filter; Hermes session APIs are operator-oriented, not integration-oriented.

### 5. Centralized team configuration

**Warp:** Per–cloud-agent identities with scoped secrets, skills, MCP servers; shared across triggers.

**Hermes today:** Single `.env` + Hermes host config; no per-team or per-workspace agent profiles in the adapter.

### 6. Multi-agent orchestration

**Warp:** Parent/child agents, fan-out, DAG workflows across runs.

**Hermes today:** One Hermes session per Linear session; concurrency cap only. README describes `CodingBridge` / parallel Claude+Codex — **not present in current `linear_agent.py`**; coding flows through Hermes tools only.

### 7. Live steerability & cross-surface session sharing

**Warp:** Attach to running cloud run, send follow-ups via API, share session link outside trigger source.

**Hermes today:** Follow-ups only via Linear `prompted` webhook; stop signal works; no external run viewer URL.

---

## Table stakes vs differentiators

| Category | Examples |
|----------|----------|
| **Table stakes** (needed for team trust at scale) | Run ID + lifecycle, prompt/command/log retention, failure reasons, cancel/stop, basic list/filter API, secrets not in prompts |
| **Table stakes for “platform”** (can defer if staying Linear-only) | Schedules, Docker environments, GitHub Action trigger, Oz-style web run catalog |
| **Differentiators Hermes already has** | Deep Linear issue graph (relations, blockers, project siblings), native Plan UI sync, workflow state nudges, homelab self-host without credit billing |
| **Differentiators Warp has** | Oz control plane, environment isolation, multi-trigger consistency, credit-based hosted scale, multi-agent orchestration |

---

## Product features vs implementation details

| User-facing product feature | Mostly implementation detail |
|-----------------------------|-------------------------------|
| “Runs” page or Linear-run history view | JSON schema for lifecycle events, DB/file backing store |
| “Replay this investigation” link | Hermes session ID = Linear session UUID mapping |
| Schedule: weekly triage on label X | Cron + webhook into same `TaskProcessor` path |
| Per-team agent profiles (model, skills, block deploy) | Env vars / Hermes config profiles |
| GitHub Action “ask Hermes” step | API wrapper around existing processor |
| Docker environment per repo | Container orchestration, image registry |
| MCP tool configuration UI | Hermes MCP config surfacing |
| Credit usage / billing dashboard | Hosting model choice |

**Principle:** Keep the **thin adapter** — new platform capabilities should land in **Hermes API** (runs, secrets, environments) with Linear adapter as one **trigger + sink**, not a second orchestrator (lesson from PLY-112).

---

## How capabilities should change Linear agent UX

| Capability | UX change |
|------------|-----------|
| **Run records** | Add stable “Hermes run” badge on agent session linking to full transcript (commands, token usage, errors) — Linear timeline stays summary-only |
| **Triggers** | Optional “Run on schedule” on Linear project/label — appears as delegated agent sessions with `source: schedule` metadata |
| **Environments** | Project setting: “Repo + image for this project’s agent runs” — reduces SSH guessing in prompts |
| **Observability** | Filter issues by “agent run failed”; ops sees cross-issue run list without opening each thread |
| **Orchestration** | Parent issue spawns linked child runs (sub-issues) with correlated run IDs — revives PLY-35 intent with real tracking |
| **Steering** | Follow-up API for non-Linear clients; Linear remains primary steering surface |

**Do not regress:** curated timeline progress (Warp transcripts can be noisy); delta prompts on follow-ups; gate-issue lightweight profile.

---

## Telemetry & audit trail for trust and debugging

### Minimum viable audit trail

| Event | Store | Expose |
|-------|-------|--------|
| `run.created` | run_id, linear_session_id, issue_id, trigger_type, user_id | Linear activity + run API |
| `run.started` / `run.completed` / `run.failed` | timestamps, duration | Run API + metrics |
| `run.prompt_sent` | hash + size (not necessarily full text in Linear) | Run detail view |
| `run.tool_invoked` | tool name, summary, exit status | Already on timeline; also structured log |
| `run.plan_updated` | todo snapshot | Plan UI + run record |
| `run.output` | response text, linked PR URLs | Linear `response` activity |
| `run.cancelled` | stop signal | Linear + run state |

### Security / compliance

- Never persist `LINEAR_API_KEY` / `HERMES_API_KEY` in run records.
- Redact secrets from command logs (Hermes privacy settings).
- Retention policy: configurable TTL for raw logs vs indefinite metadata.
- IP / webhook dedup already aid abuse prevention; add run-level rate limits per team.

### Metrics (ops)

- Runs/hour, p95 duration, failure rate by trigger type, tool error rate, Hermes API latency (see `reports/ply-17-hermes-latency-report.md`).
- Concurrent sessions gauge (`_active_runs` → exported metric).

---

## Proposed first slice (minimal end-to-end worth shipping)

### Slice: **Hermes Run Registry + Linear correlation** (PLY-165a)

**Goal:** One durable `run_id` per Linear agent turn (or per session — decide below) with lifecycle API, without changing the thin-adapter architecture.

**Scope:**

1. **Hermes API** (or adapter-side interim): `POST /v1/runs`, `PATCH /v1/runs/{id}`, `GET /v1/runs`, `GET /v1/runs/{id}`
   - Fields: `run_id`, `state`, `trigger` (`linear.session.created` | `linear.session.prompted` | …), `linear_session_id`, `issue_id`, `hermes_session_id`, `started_at`, `ended_at`, `error`, `metadata` (model, team_id)
2. **Adapter hooks** in `TaskProcessor` / `AgentWebhookHandler`:
   - Create run on task start; transition on complete/fail/cancel
   - Append structured tool events from existing SSE parser (parallel to DiscoveryTracker)
3. **Linear UX (light):**
   - Final `response` activity footer: `Run: <short-id>` linking to Hermes dashboard or `GET /v1/runs/{id}` JSON
4. **Ops:**
   - `journalctl` correlation via `run_id` in log context

**Out of scope for slice 1:** schedules, Docker environments, multi-agent DAG, new web UI, billing.

**Success criteria:**

- Operator can answer “what did the agent do on PLY-XX last Tuesday?” without exporting Hermes session JSON manually
- Failed runs have structured `error` + last tool invocation
- Restarting `linear-agent` does not lose in-flight run metadata (persisted store)

**Estimated invasiveness:** Small–medium in adapter; medium in Hermes API if run store lives there (preferred).

### Slice 2 (fast follow): **Generic webhook trigger** (PLY-165b)

`POST /v1/runs` with `{ "prompt", "issue_id"?, "callback"? }` for CI/GitHub Action — reuses `TaskProcessor` headless path, creates Linear session optionally.

### Slice 3 (later): **Scheduled triage** (PLY-165c)

Hermes cron or external scheduler → slice 2 API with Linear label/filter query.

---

## Constraints: permissions, cost, hosting, security

| Constraint | Hermes Linear agent implication |
|------------|----------------------------------|
| **Permissions** | Linear OAuth app is issue-scoped; agent cannot admin workspace. Gate issues use prompt profile, not ACL system. Team allowlist via `LINEAR_TEAM_IDS`. |
| **Cost** | Self-hosted: VPS + LLM API costs (no Warp credits). Risk: unbounded tool loops — need run timeouts and concurrency cap (have cap=10). |
| **Hosting** | Single-node homelab today; Cloudflare Tunnel for webhooks. No worker pool — horizontal scale = multiple adapters + sticky session routing or queue. |
| **Security** | HMAC + IP allowlist strong for inbound webhooks. Outbound: Hermes executes arbitrary shell — trust model = “agent host is trusted compute.” Environments would reduce blast radius. |
| **Network boundary** | All execution on operator network (feature for compliance). Warp self-hosted workers are analog; we already match “unmanaged mode” partially. |
| **Data residency** | Issue text flows to Hermes/LLM provider; run store location TBD. |
| **Billing model** | Warp charges credits for cloud runs; Hermes avoids this but operators pay inference directly — product implication: cost visibility per run is valuable. |

---

## Adjacent capabilities to consider while scoping

| Adjacent | Relevance |
|----------|-----------|
| **Slack gateway** (`docs/PLY-153-slack-gateway-learnings.md`) | Same thin-adapter pattern; shared run registry across surfaces |
| **Cursor cloud agents** | Already used for coding PRs; bridge via explicit issue link + run correlation, not duplicate orchestration |
| **Hermes dashboard** | Natural home for run catalog UI |
| **Linear Diffs / PR linking** | Already integrated; extend with run_id in PR body template |
| **Paperclip / plugins** (`docs/PLY-48-paperclip-linear-plugin-home-lab.md`) | Plugin triggers = another run source |
| **Issue documents** (rich blocks) | Context gap vs Warp codebase context — backlog item |
| **Subagent / CodingBridge** (PLY-35) | Re-scope as child **runs** with parent `run_id` reference |
| **Agent secrets vault** | Hermes `secrets` CLI — expose scoped injection per run |
| **MCP server registry** | Align with Warp centralized MCP; Hermes-native |

---

## Open questions

1. **Run granularity:** One run per Linear `AgentSession` lifetime, or per turn (`created` / `prompted`)? Per-turn matches Warp; per-session simplifies Hermes memory alignment.
2. **Run store location:** Hermes API server vs adapter-local SQLite vs Linear custom fields?
3. **Transcript authority:** Is Hermes session JSON the source of truth, with run record as index — or duplicate summary into run store?
4. **Public run links:** Auth model for sharing run detail outside Linear (dashboard OAuth vs signed URLs)?
5. **Environment MVP:** Is “git worktree per run” on existing host enough before full Docker?
6. **CodingBridge:** Revive as Hermes-delegated child run, or drop README claims?
7. **Schedule UX:** Linear-native (recurring issue templates) vs Hermes cron vs external GitHub Actions?
8. **Multi-tenant:** Multiple Linear workspaces on one agent host — config isolation strategy?

---

## Recommended follow-up issues

| ID | Title | Priority |
|----|-------|----------|
| **PLY-165a** | Hermes `/v1/runs` lifecycle API + Linear adapter correlation | **P0** — first slice |
| **PLY-165b** | Headless run trigger API (CI / generic webhook) | P1 |
| **PLY-165c** | Scheduled agent runs (label/project triage) | P2 |
| **PLY-165d** | Run detail UI in Hermes dashboard (list, filter, transcript) | P1 (depends on 165a) |
| **PLY-165e** | Structured tool audit log (parallel to DiscoveryTracker) | P1 |
| **PLY-165f** | Execution environments MVP (worktree or Docker) | P2 |
| **PLY-165g** | Reconcile CodingBridge README with implementation | P2 |
| **PLY-165h** | Per-run cost/token metrics on run record | P2 |
| **PLY-165i** | Multi-agent child runs (parent/child run_id, sub-issues) | P3 |

---

## Answers to issue questions (checklist)

| Question | Answer |
|----------|--------|
| What do we already support? | Linear triggers, live timeline progress, plans, rich context, workflow nudges, PR links, webhook security, thin Hermes adapter |
| Biggest gaps? | Run registry, APIs, environments, cross-trigger scheduling, team run catalog |
| Table stakes vs differentiators? | Run audit trail = table stakes; Docker fan-out = platform differentiator; Linear graph context = Hermes differentiator |
| Product vs implementation? | Runs page = product; event schema = implementation |
| UX impact? | Run links + ops catalog; keep timeline curated |
| Telemetry needed? | Lifecycle events, tool audit, token/cost, correlation IDs |
| Minimal first ship? | **PLY-165a** run registry + Linear correlation |
| Constraints? | Self-hosted trust model, Linear ACLs, LLM cost, single-node scale |
| Adjacent capabilities? | Slack gateway, dashboard, Cursor bridge, MCP/secrets centralization |

---

## Paste-ready Linear comment

> **PLY-165 investigation complete.** Compared Warp Oz cloud agents to Hermes Linear agent. We already match well on Linear-native triggers, live tool progress, plans, and issue-context depth. Main gaps vs a true agent platform: **durable run records**, **programmatic run APIs**, **schedules/generic triggers**, and **reproducible environments**. Recommended first slice: **Hermes `/v1/runs` + Linear session correlation** (backlog item now filed as PLY-165a). Full map: `docs/PLY-165-warp-cloud-agents-capability-map.md`.
