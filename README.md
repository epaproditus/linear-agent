# Linear AI Agent

Hermes-powered autonomous agent for Linear. Integrates with Linear's Agent Session API so you can @-mention the agent in issues, delegate tasks to it, and have it respond with typed activities.

## Architecture

```
Linear Webhook ──POST──► FastAPI (port 8660)
                              │
                     ┌───────┴───────┐
                     │  HMAC verify   │
                     │  IP allowlist  │
                     └───────┬───────┘
                             │
                     ┌───────┴───────┐
                     │  Event Router │
                     └───────┬───────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
  AgentSessionEvent      Comment            Issue
  (created/prompted)   (@mention)      (assign/delegate)
         │                   │                   │
         └───────────────────┼───────────────────┘
                             │
                     ┌───────┴───────┐
                     │ TaskProcessor  │
                     │  (asyncio)     │
                     └───────┬───────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        Linear GraphQL  Hermes API    CodingBridge
        (comments,     (LLM reasoning)  (Claude/Codex)
         updates,
         activities)

```

### Components

| Component | Description |
|-----------|-------------|
| **FastAPI app** (`linear_agent.py`) | Webhook receiver on port 8660. Handles HMAC verification, IP allowlist, event routing |
| **LinearClient** | Async GraphQL client for Linear API. Issues, comments, workflow states, agent activities |
| **TaskProcessor** | Orchestrates task execution. Fetches issue details, runs LLM reasoning, emits activities |
| **CodingBridge** | Optional: delegates coding tasks to Claude Code or Codex CLI |
| **AgentWebhookHandler** | Routes webhook events. Deduplication, self-loop prevention, team allowlist |

### Event Flow

1. **AgentSessionEvent (created)**: User @-mentions agent or delegates an issue. Agent acknowledges within 10s (via external URL update), fetches issue, runs LLM, emits response activity
2. **AgentSessionEvent (prompted)**: Follow-up message in an existing agent session. Agent reconstructs conversation from previous activities, runs LLM, responds
3. **Comment (create)**: Direct @mention in an issue comment. Creates a proactive agent session, then processes normally
4. **Issue (update)**: Assignment or delegation to the agent. Creates agent session and begins work

## Subagent Integration (PLY-35)

When the agent detects a **coding task**, it delegates to a subagent (Claude Code or Codex CLI) instead of using LLM reasoning. Detection is based on issue labels, title keywords, and description keywords.

### Detection Heuristics

| Trigger | Example | Routes to |
|---------|---------|-----------|
| Label is Bug/Feature/Enhancement/Development | `Bug` label | CodingBridge |
| 2+ coding keywords in title+description | "Implement login API" | CodingBridge |
| 1 coding keyword in title | "Fix bug" | CodingBridge |
| CODING_AGENT=none | — | Always LLM |
| No matches | "Write documentation" | Always LLM |

### Delegation Flow

1. **Child issue created** — A linked sub-issue `[Subagent] <title>` is created in Linear to track the subagent's work
2. **Context assembled** — Issue title, description, labels, and the user's message are bundled as custom instructions
3. **Subagent spawned** — The coding CLI runs with the full task context
4. **Result posted** — Output is added as a comment on the parent issue
5. **Review-before-merge** — Parent issue moves to "In Review" state automatically
6. **Child issue updated** — Sub-issue reflects the completion or failure status

### Subagent Types

| Backend | CLI Command | Install |
|---------|-------------|---------|
| `claude` (default) | `claude --print "prompt"` | `npm install -g @anthropic/claude-code` |
| `codex` | `codex exec "prompt"` | `npm install -g @openai/codex` |
| `all` | Both (parallel) | Both above |

Set `CODING_AGENT=all` to run both Claude Code and Codex CLI in parallel. Results from both are collected independently and reported.

### Features Implemented

- **Parallel subagent execution** (`CodingBridge.run_parallel`) — Run multiple coding agents simultaneously
- **Custom subagent instructions** — User's message is passed verbatim as custom instructions to the subagent
- **Review-before-merge** — Issues auto-transition to "In Review" after subagent completes
- **Child issue tracking** — Each subagent delegation creates a tracked sub-issue in Linear

### Activity Types

The agent emits typed activities to the session for transparency:

- **thought** (ephemeral): Thinking indicator, updates every 10s during LLM processing
- **action** (ephemeral): Tool calls (e.g. "Searching files...", "Running shell command...")
- **response** (persistent): Final response with results
- **error** (persistent): Error message if something fails
- **elicitation**: Request for clarification or auth (not currently used by default)

## Repository Structure

```
/home/abe/linear-agent/
├── linear_agent.py              # Linear agent (port 8660)
├── plane_agent.py               # Plane agent (port 8648)
├── pyproject.toml               # Project metadata & dependencies
├── requirements.txt             # Python dependencies (legacy)
├── .env.example                 # Configuration template
├── .gitignore
├── linear-agent-user.service    # systemd user unit
├── bin/
│   ├── linear-agent-wrapper.sh  # Wrapper with systemd-notify watchdog
│   └── update-service.sh        # Install/update systemd unit
├── tests/                       # Test suite
│   └── test_pr_urls.py
├── scripts/                     # Utility scripts
├── docs/                        # Documentation and reports
├── plans/                       # Implementation plans
└── reports/                     # Analysis reports
```

## Deployment

The agent runs as a systemd **user** service — no root required.

```bash
# Install/update the service
bash ~/linear-agent/bin/update-service.sh

# Or manually:
cp linear-agent-user.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart linear-agent

# Check status
systemctl --user status linear-agent

# View logs
journalctl --user -u linear-agent -f
```

### Port

| Agent | Port |
|-------|------|
| Linear Agent | 8660 |
| Plane Agent | 8648 |
| Hermes API Server | 8642 |

## Configuration

Copy `.env.example` to `.env` and set required values:

| Variable | Required | Description |
|----------|----------|-------------|
| `LINEAR_API_KEY` | Yes | OAuth token for Linear GraphQL API |
| `LINEAR_WEBHOOK_SECRET` | Yes | HMAC shared secret for webhook verification |
| `HERMES_API_KEY` | Yes | API key for Hermes LLM backend |
| `LINEAR_AGENT_BOT_NAME` | No | Bot display name (default: "Hermes") |
| `LINEAR_AGENT_USER_ID` | No | Auto-detected; set to prevent self-loop |
| `LINEAR_TEAM_IDS` | No | Comma-separated team allowlist |
| `LINEAR_ENFORCE_IP_ALLOWLIST` | No | Disable behind reverse proxy (default: true) |
| `HERMES_API_URL` | No | LLM endpoint (default: http://127.0.0.1:8642/v1) |
| `HERMES_MODEL` | No | Model name (default: hermes-agent) |
| `CODING_AGENT` | No | Backend: claude, codex, all, or none (default: claude) |

## Security

- **HMAC-SHA256**: Every webhook is verified against a shared secret
- **IP Allowlist**: Only known Linear webhook IPs accepted (optional, disabled behind reverse proxy)
- **Team Allowlist**: Restrict which teams the agent can act on
- **Self-loop Prevention**: Agent ignores its own comments and activities
- **No API keys in prompts**: Environment variables referenced, never interpolated into LLM context

## Development

```bash
# Create virtual environment
uv venv .venv
source .venv/bin/activate

# Install dependencies
uv pip install -r requirements.txt

# Run locally
uvicorn linear_agent:app --host 0.0.0.0 --port 8660 --reload

# Run with wrapper (systemd-notify watchdog)
bash bin/linear-agent-wrapper.sh
```

## Related

- [Hermes Agent](https://hermes-agent.nousresearch.com) — The AI agent framework powering this
- [Linear Agent Session API](https://developers.linear.app/docs/agent/agent-sessions) — Linear's agent integration docs
- [Plane Agents](https://developers.plane.so/dev-tools/agents/building-an-agent) — Plane's agent integration docs

## Plane Agent

A companion Plane agent runs on port **8648**. It mirrors the Linear agent's architecture but uses Plane's REST API instead of Linear's GraphQL.

### Architecture

```
Plane Webhook ──POST──► FastAPI (port 8648)
                              │
                     ┌───────┴───────┐
                     │  HMAC verify   │
                     │  Rate limiter  │
                     └───────┬───────┘
                             │
                     ┌───────┴───────┐
                     │  Event Router │
                     └───────┬───────┘
                             │
                     ┌───────┴───────┐
                     │ TaskProcessor  │
                     │  (asyncio)     │
                     └───────┬───────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
        Plane REST API  Hermes API    DiscoveryTracker
        (activities,   (LLM reasoning) (progress updates)
         work items,
         comments)
```

### Key Differences from Linear Agent

| Aspect | Linear Agent | Plane Agent |
|--------|-------------|-------------|
| API style | GraphQL (api.linear.app/graphql) | REST (api.plane.so/api/v1/) |
| Port | 8660 | 8648 |
| Auth | OAuth token | Bot token (Bearer) |
| Entity | AgentSession | AgentRun |
| Activity | AgentActivityCreate mutation | POST /agent-runs/{id}/activities/ |
| Workspace | Team-based routing | Workspace slug required in URL |
| Issue naming | Issues | Work Items |
| SDK | None (raw GraphQL) | plane-sdk (pip install plane-sdk) |

### Setup

1. Create an OAuth app in Plane with **Enable App Mentions** and **Agent Run** scopes
2. Register these URLs in the OAuth app (must be publicly reachable):
   - **Setup URL:** `https://<public-host>/plane/install`
   - **Redirect URI:** `https://<public-host>/plane/oauth/callback`
   - **Webhook URL:** `https://<public-host>/plane/webhook`
3. Configure the agent via `.env` (bot token not required yet):
   ```
   PLANE_WEBHOOK_SECRET=<webhook_secret>
   PLANE_CLIENT_ID=<client_id>
   PLANE_CLIENT_SECRET=<client_secret>
   PLANE_PUBLIC_URL=https://<public-host>
   ```
4. Start the agent:
   ```bash
   uvicorn plane_agent:app --host 0.0.0.0 --port 8648
   ```
5. In Plane, click **Install** on your OAuth app. The Setup URL redirects to Plane's consent screen; after approval, the callback exchanges the installation ID for a bot token and writes `PLANE_API_KEY` to `.env`.

After installation, these values are populated automatically:
```
PLANE_API_KEY=<bot_token>
PLANE_APP_INSTALLATION_ID=<installation_id>
PLANE_WORKSPACE_SLUG=<workspace_slug>
```

### Webhooks

The agent handles two webhook events:
- **agent_run** (action: created) — New agent run when user @-mentions the agent
- **agent_run_activity** (action: prompted) — Follow-up message in an existing run

Activities are created using Plane's REST API at:
```
POST /api/v1/workspaces/{slug}/agent-runs/{run_id}/activities/
```

### Implementation Notes

- Uses `httpx` AsyncClient for REST calls (same dependency as Linear agent)
- Activity types: thought, action, response, error, elicitation
- Supports ephemeral activities (thought, action) for progress visibility
- Accepts both `X-Plane-Signature` and `X-Hub-Signature-256` webhook headers
- Workspace slug is resolved from config or fetched from Plane API
- Can target self-hosted Plane instances via `PLANE_API_URL`
