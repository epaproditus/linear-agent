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
├── linear_agent.py              # Main application (1875 lines)
├── pyproject.toml               # Project metadata & dependencies
├── requirements.txt             # Python dependencies (legacy)
├── .env.example                 # Configuration template
├── .gitignore
├── linear-agent-user.service    # systemd user unit
├── bin/
│   ├── linear-agent-wrapper.sh  # Wrapper with systemd-notify watchdog
│   └── update-service.sh        # Install/update systemd unit
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
| `CODING_AGENT` | No | Backend: claude, codex, or none (default: claude) |

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
- [Plane Agent](https://plane.epaphrodit.us) — Companion project management agent
