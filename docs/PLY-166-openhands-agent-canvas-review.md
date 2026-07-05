# PLY-166 — OpenHands Agent Canvas review for Hermes

**Status:** investigation complete (2026-07-05)  
**Audience:** Hermes / Linear agent product and engineering  
**Starting point:** [OpenHands Agent Canvas overview](https://docs.openhands.dev/openhands/usage/agent-canvas/overview)

---

## Executive summary

**Agent Canvas is not a spatial “canvas” UI.** It is OpenHands’ unified **browser control plane** for running coding agents and automations: one frontend talks to swappable backends (local VM, Docker, Modal, Cloud), drives conversations with OpenHands or ACP agents (Claude Code, Codex, Gemini CLI), and layers **cron + event automations** on top. Trust comes from **inspectable execution surfaces** (chat, file diffs, terminal, browser, app preview) and **repeatable workflows** (templates, saved settings, conversation history).

**Hermes-as-Linear-agent** already covers a overlapping slice: event-triggered work (@mention / delegate), live tool progress on a timeline, plan checklist projection, session continuity, and PR artifact linking. Hermes deliberately **projects onto Linear’s Agent Session UI** instead of building its own canvas.

The highest-leverage ideas to adapt first are **(1) backend/agent portability patterns**, **(2) first-class automation templates beyond ad-hoc @mentions**, **(3) richer inspection affordances within Linear’s constraints**, and **(4) explicit Customize vs Settings separation** for skills/MCP vs runtime config. Ideas to **avoid or defer**: rebuilding OpenHands’ multi-tab workspace inside Linear, full cron automation platform in the adapter, and duplicating ACP multi-agent switching in a channel where Linear already owns the session chrome.

---

## What Agent Canvas is trying to make easier / more trustworthy

| Goal | How Canvas addresses it |
|------|-------------------------|
| **One place to run any agent** | Single UI + ACP for OpenHands, Claude Code, Codex, Gemini CLI |
| **Run anywhere** | Frontend/backend split; local, VM, Docker, Modal, Cloud backends |
| **Bring your own model** | Per-backend LLM profiles; `/model` switching mid-conversation |
| **Prove what the agent did** | Changes tab, embedded VS Code, terminal, browser, app preview |
| **Repeatable ops** | Automations (cron + GitHub/Linear/Slack/webhook events), pre-built templates |
| **Teach the agent** | Skills + MCP under **Customize**; secrets/condenser/critic under **Settings** |
| **Safe remote access** | `--public` + API key, ngrok OAuth, reverse proxy, VM hardening checklist |

Core trust model: the user can **watch execution** (tools, files, terminal) and **replay conversations** after automations run—not just read a final summary.

---

## Key UX concepts, workflows, and primitives

### Primitives

| Primitive | Meaning |
|-----------|---------|
| **Agent Canvas** | npm/Docker stack: UI + agent server + automation service |
| **Backend** | Agent server + workspace; owns settings, secrets, conversations, automations |
| **Conversation** | One agent session: messages, tool calls, file changes |
| **Automation** | Cron or event-triggered agent run; may spawn conversations |
| **ACP agent** | External CLI agent subprocess (Claude Code, Codex, Gemini) via JSON-RPC |
| **Customize** | Skills, MCP servers (capability injection) |
| **Settings** | Agent behavior, LLM, condenser, verification/critic, secrets |
| **LLM profile** | Saved provider/model/key bundle; switchable with `/model` |
| **Workspace** | Optional folder binding before a conversation starts |

### Primary workflows

1. **Install → onboard wizard** — choose agent → verify backend → LLM/credentials → start from template automation.
2. **Interactive coding** — open workspace → chat → inspect Changes / Terminal / Browser / App tabs.
3. **Backend switching** — Manage Backends; all config scopes to active backend.
4. **Cron automation** — natural-language creation via Automation Skill; saved runs appear in conversation list.
5. **Event automation** — GitHub built-in; custom webhooks (Linear walkthrough documented) with JMESPath filters.
6. **Pre-built templates** — e.g. GitHub PR Review: MCP setup + prefilled agent conversation to configure automation.

### Experience structure (information architecture)

```
Home / Conversations
├── Chat panel (reasoning + actions)
├── Changes | VS Code | Terminal | Browser | App  ← inspection rails
├── Automate (templates + automation list)
├── Customize (Skills, MCP)
└── Settings (Agent, LLM, Condenser, Verification, Secrets)
     └── Backend switcher (bottom-left)
```

---

## Pages reviewed

### Agent Canvas (core)

| Page | URL |
|------|-----|
| Overview | https://docs.openhands.dev/openhands/usage/agent-canvas/overview |
| Install | https://docs.openhands.dev/openhands/usage/agent-canvas/setup |
| First Time Setup | https://docs.openhands.dev/openhands/usage/agent-canvas/first-time-setup |
| Backends | https://docs.openhands.dev/openhands/usage/agent-canvas/backends |
| VM / Self-Hosted | https://docs.openhands.dev/openhands/usage/agent-canvas/backend-setup/vm |
| Customize and Settings | https://docs.openhands.dev/openhands/usage/agent-canvas/customize-and-settings |
| ACP Agents | https://docs.openhands.dev/openhands/usage/agent-canvas/acp-agents |
| LLM Profiles | https://docs.openhands.dev/openhands/usage/agent-canvas/llm-profiles |
| GitHub PR Review (pre-built) | https://docs.openhands.dev/openhands/usage/agent-canvas/prebuilt/github-pr-review |

### Automations

| Page | URL |
|------|-----|
| Automations Overview | https://docs.openhands.dev/openhands/usage/automations/overview |
| Event-Based Automations | https://docs.openhands.dev/openhands/usage/automations/event-automations |

### Platform context

| Page | URL |
|------|-----|
| Key Features (legacy GUI tabs) | https://docs.openhands.dev/openhands/usage/key-features |
| Backend Architecture | https://docs.openhands.dev/openhands/usage/architecture/backend |
| Linear Integration (Cloud, coming soon) | https://docs.openhands.dev/openhands/usage/cloud/project-management/linear-integration |

*Note: `prebuilt-automations` index and `critic` pages timed out during fetch; covered via first-time-setup and PR review pages.*

---

## Comparison: Hermes today vs Agent Canvas

### What we already do that is similar

| Canvas concept | Hermes / Linear agent today |
|----------------|----------------------------|
| Event-triggered agent work | Linear webhooks: `created`, `prompted`, @mention, delegate |
| Conversation per work unit | One Hermes session per Linear `AgentSession` (native mode) |
| Live execution visibility | `hermes.tool.progress` → `DiscoveryTracker` → timeline `thought` activities |
| Plan checklist | Hermes `todo` → `GET /api/todos` → Linear Agent Plans API |
| External agent delegation | Hermes API server-side tools (incl. coding); README mentions CLI bridge (not in adapter code) |
| Skills | Hermes host skills (native mode: agent-owned; legacy: adapter injects catalog) |
| Bring-your-own LLM | Hermes API server model config (not exposed in Linear UI) |
| Final artifact linking | GitHub PR URLs → `externalUrls` / Linear Diffs |
| Human-in-the-loop | Follow-up prompts, stop signal, gate-issue profile, blocker deferral |
| Remote backend pattern | Hermes on VPS + Cloudflare tunnel; thin `linear_agent.py` adapter |
| Two-phase output quality | Phase-2 conclusions-only rewrite after tool progress |
| Session keepalive | Background activity + content-drought keepalive during long runs |

Hermes is closest to Canvas’s **event automation on Linear** path (see Event-Based Automations Linear walkthrough)—but implemented as a **native Linear Agent** rather than a webhook into a separate Canvas UI.

### What is novel or especially strong in Agent Canvas

1. **Unified control plane** — conversations + automations + customization in one product surface.
2. **Backend portability** — same UI against local, VM, cloud sandboxes; config scoped per backend.
3. **ACP agent multiplexing** — swap OpenHands vs Claude Code vs Codex without changing UI metaphors.
4. **Inspection rails** — file diff, VS Code, terminal, browser, running app are first-class tabs (not timeline prose).
5. **Automation factory** — NL-created cron jobs + JMESPath event filters + template library.
6. **Onboarding as workflow selection** — wizard ends in a **proven automation**, not a blank chat.
7. **Mid-conversation model switch** — `/model` with visible timeline events.
8. **Conversation replay for automations** — scheduled runs are reviewable/continuable conversations.

### Assumptions Canvas makes

| Dimension | Assumption |
|-----------|------------|
| **User** | Developer/operator at a keyboard; comfortable with API keys, MCP, VM setup |
| **Environment** | Trusted machine with filesystem + shell access; sandbox optional but local trust default |
| **Workflow** | Long interactive sessions at a desk; automations for recurring eng-ops |
| **Channel** | User comes to OpenHands UI (push), not issue tracker (pull) |
| **Inspection** | User will switch tabs to verify file/terminal/browser state |
| **Integrations** | GitHub/Slack/Linear are **sources** that kick off Canvas conversations |
| **Org model** | Team orgs + claimed GitHub orgs for event routing (Cloud) |

Hermes assumes the **inverse channel**: user stays in Linear; agent comes to the issue; inspection is **timeline + plan + linked PR**, not a multi-tab IDE.

### Supporting features needed for Canvas to work well

- Agent server + runtime (Docker/local/remote) with action execution
- Persistent settings/secrets store per backend
- MCP + skills registry
- WebSocket/SSE for live events (nginx `proxy_read_timeout` guidance)
- Automation service (cron scheduler, webhook registry, JMESPath filters)
- Git provider OAuth for default token access
- Strong API key / OAuth perimeter for `--public` deployments

Hermes already depends on a smaller subset: Hermes API SSE, Linear GraphQL activities, HMAC webhooks, tunnel.

---

## Existing / missing / maybe-later for Hermes

### ✅ Existing (keep investing)

- Thin adapter architecture (Hermes owns agent loop; Linear owns presentation)
- Native mode: one session ID, todo→plan sync
- Tool progress beautification + rate limiting
- Conversation watermarks for prompted turns
- Project/sibling/relations context injection (PLY-78)
- PR external URLs for Diffs
- Gate-issue lightweight profile

### ❌ Missing (high signal gaps vs Canvas)

| Gap | Impact |
|-----|--------|
| **No response token streaming** to Linear timeline | Less “alive” during final answer assembly |
| **No inspection rails** (diff/terminal/browser in-session) | Trust relies on prose + PR link |
| **No automation layer** beyond reactive @mention | No scheduled triage, standup digests, etc. |
| **No user-facing model/profile switch** in Linear | Model changes require Hermes host config |
| **Elicitation API wired but unused** | Missed structured HITL (auth/select) |
| **Subagent/CodingBridge not in adapter** | No swimlanes for parallel child work |
| **No conversation fork/resume UX** | Linear session is linear history only |
| **Critic / verification loop** not exposed | No iterative refinement surface in Linear |

### 🔜 Maybe later (adapt with modification)

| Canvas idea | Hermes adaptation |
|-------------|-------------------|
| Pre-built automation templates | Linear **issue templates** or label-triggered playbooks (“triage”, “PR review”) |
| Event filters (JMESPath) | Richer webhook routing: team, label, project, priority |
| `/model` profiles | Issue-level or project-level model hints via description/guidance |
| Customize vs Settings split | Document operator vs developer config; expose skills discovery in responses |
| Backend switcher | N/A in Linear; but **multi-host Hermes routing** by team/project could mirror this |
| Critic / verification | Phase-2 finalize is a light version; could add explicit “verify” step in plan |
| Browser/terminal replay | Link out to Hermes session dump or dashboard URL in `externalUrls` |
| Cloud Linear integration | OpenHands Cloud Linear path is “coming soon”—Hermes is ahead on native Agent API |

### ⛔ Avoid (poor fit for Linear channel)

- Building Canvas-style multi-tab workspace inside Linear Agent Session
- Running full cron automation platform inside `linear_agent.py`
- Re-implementing ACP agent picker in Linear (Hermes already abstracts providers)
- Duplicating OpenHands sandbox management in the adapter
- Streaming raw tool JSON to timeline (Canvas shows structured events; we already beautify—keep that)

---

## Highest-leverage ideas to bring into Hermes first

### 1. Template-triggered workflows (Canvas “proven workflow” onboarding)

**What:** Label or issue-template triggers with prefilled Hermes intent (e.g. `agent:triage`, `agent:pr-review`), similar to Canvas pre-built automations.

**Why:** Reduces blank-thread cold start; matches Canvas step-4 wizard without leaving Linear.

**Effort:** Low–medium (webhook filter + prompt preset library).

### 2. Richer inspection without a custom canvas

**What:** When tools produce artifacts, attach **structured external links**: PR (done), CI run, Hermes session replay URL, key file deep links, optional screenshot.

**Why:** Canvas trust comes from inspection rails; Linear’s equivalent is **timeline + externalUrls + Reviews**.

**Effort:** Medium (Hermes session export endpoint + adapter linking).

### 3. Wire elicitation for real HITL

**What:** Use `send_elicitation` / `send_elicitation_select` when Hermes needs auth, scope choice, or disambiguation.

**Why:** Canvas assumes desktop user nearby; Linear users need **in-channel prompts** instead of terminal prompts.

**Effort:** Medium (map Hermes clarify events → Linear elicitation activities).

### 4. Quiet vs verbose progress modes

**What:** Canvas conversations are fully inspectable; Linear needs **sparse milestone mode** for long runs (PLY-40 intent).

**Why:** Timeline noise erodes trust; Canvas solves via tabs—we solve via curation tiers.

**Effort:** Low (adapter config + DiscoveryTracker policy).

### 5. Explicit automation catalog (companion to reactive agent)

**What:** Document and optionally implement **scheduled** Hermes jobs (cron on VPS) that post to Linear issues—mirror Canvas cron automations but keep execution on Hermes host.

**Why:** Canvas’s second pillar after chat; Hermes today is almost entirely reactive.

**Effort:** Medium–high (new service; not in adapter).

---

## How this should change Hermes presentation

| Area | Current | Recommended shift |
|------|---------|-------------------|
| **Planning** | Hermes `todo` → Linear plan (good) | Add template-named plan seeds for known workflows |
| **Execution** | Tool progress prose | Milestone tiers + link-out artifacts |
| **Context** | Rich issue card on `created` | Optional “workspace binding” hint (repo path in project guidance) |
| **Inspection** | Timeline + PR Diffs | `externalUrls` for CI, session replay, docs |
| **Collaboration** | Thread follow-ups | Elicitation for choices; @mention routing filters |

---

## Recommended follow-up issues

| ID (proposed) | Title | Rationale |
|---------------|-------|-----------|
| PLY-167 | Label/template-triggered Hermes workflow presets | Canvas onboarding via proven workflows |
| PLY-168 | Wire Linear elicitation activities to Hermes clarify flows | In-channel HITL vs desktop assumption |
| PLY-169 | Artifact linking: session replay + CI URLs in externalUrls | Inspection without custom canvas |
| PLY-170 | DiscoveryTracker sparse/verbose progress profiles | Long-run timeline ergonomics |
| PLY-171 | Scheduled Hermes automations that post to Linear | Canvas cron pillar |
| PLY-172 | Subagent progress surfacing (child issue or timeline swimlanes) | Canvas multi-conversation visibility |
| PLY-173 | Project-level model/profile hints for Hermes | Canvas `/model` profiles, Linear-native |
| PLY-174 | Evaluate OpenHands Cloud Linear integration vs Hermes positioning | Competitive / integration overlap |

---

## Bottom line

OpenHands Agent Canvas optimizes for **developer control at the desktop**: one UI, many backends, deep inspection, and automations as first-class citizens. Hermes optimizes for **issue-native agent work**: meet the user in Linear, stream trustworthy progress, project plans, and land conclusions with citations.

The strategic overlap is **event-driven agent work on engineering artifacts** (issues, PRs, repos). The strategic divergence is **where inspection and configuration live**. Hermes should not become Canvas inside Linear; it should **borrow Canvas’s workflow templates, automation thinking, and inspection linking** while doubling down on thin-adapter strengths: native Linear Agent Session, Hermes-owned execution, and curated timeline narrative.
