# Linear AI Agent

A full-featured Linear agent that receives issues via Linear's Agent Session API,
processes them autonomously, and optionally delegates coding tasks to Claude Code or Codex CLI.

## Architecture

```
Linear (user @mentions agent or delegates issue)
  │
  ▼  POST /linear/webhook (AgentSessionEvent)
Agent Service (port 8660)
  │
  ├── 1. HMAC-SHA256 verify → IP allowlist → Timestamp check
  ├── 2. Parse promptContext (issue, comments, guidance)
  ├── 3. Acknowledge with `thought` activity (within 10s)
  ├── 4. Process task
  │     ├── Fetch full issue from Linear GraphQL API
  │     ├── Analyze: title, description, labels, priority
  │     ├── If coding task → delegate to Claude Code / Codex CLI
  │     └── If analysis → summarize, comment, update
  ├── 5. Emit `action` activities for progress
  ├── 6. Emit `response` activity with result
  └── 7. Update issue (comment, status, etc.)
```

## Setup

### 1. Create a Linear OAuth App

1. Go to [Linear Settings > API > Applications > New](https://linear.app/settings/api/applications/new)
2. Name your agent (e.g. "Hermes")
3. Enable **Webhooks** and check ☑ **Agent session events**
4. Set the webhook URL to `https://your-host:8660/linear/webhook`
   (or use Cloudflare tunnel: `https://webhooks.epaphrodit.us/linear/webhook`)
5. Under **Authentication**, use **Client Credentials**:
   ```bash
   curl -X POST https://api.linear.app/oauth/token \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "grant_type=client_credentials&client_id=YOUR_CLIENT_ID&client_secret=YOUR_CLIENT_SECRET&scope=read,write,app:assignable,app:mentionable"
   ```
6. Save the returned **access token** and the **webhook signing secret**

### 2. Configure

```bash
cd ~/linear-agent
cp .env.example .env
# Edit .env:
#   LINEAR_API_KEY      = the OAuth access token from step 1
#   LINEAR_WEBHOOK_SECRET = the webhook signing secret
#   CODING_AGENT        = claude (default) or codex or none
```

### 3. Install & Run

```bash
# Option A: Manual
cd ~/linear-agent
source .venv/bin/activate
uvicorn linear_agent:app --host 0.0.0.0 --port 8660

# Option B: systemd service
sudo cp linear-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now linear-agent
```

### 4. Expose via Cloudflare Tunnel

Add this to your Cloudflare tunnel config:

```yaml
- hostname: webhooks.epaphrodit.us
  service: http://localhost:8660
  path: /linear/*
```

### 5. Test It

```bash
# Health check
curl http://localhost:8660/health

# @-mention @Hermes in a Linear issue comment
# Or delegate an issue to the agent
```

## How It Works

| Trigger | What Happens |
|---------|-------------|
| @-mention in Linear comment | Agent session created → agent analyzes the issue → replies with analysis |
| Issue delegated to agent | Agent session created → agent processes the task → comments result |
| Issue assigned to agent | Agent session created → agent picks it up → updates status |

## Coding Agent Integration

When the agent detects a development task (bug, feature, refactor), it routes to:

- **Claude Code** (default) — `claude --print "prompt"` with 5-min timeout
- **Codex CLI** — `codex exec "prompt"` (install with `npm install -g @openai/codex`)

The coding agent receives the issue title, description, and labels as context.
Results are posted back to the issue as comments.

## File Structure

```
~/linear-agent/
├── linear_agent.py       # Main agent service (all-in-one)
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variables template
├── .env                  # Your credentials (gitignored)
├── linear-agent.service  # systemd unit
├── setup.sh              # One-command setup
└── workspace/            # Coding agent working directory
```

## Key Security Features

- **HMAC-SHA256** signature verification on every webhook
- **IP allowlist** — only Linear's published IPs can POST webhooks
- **Timestamp validation** — prevents replay attacks
- **Self-loop prevention** — won't reply to its own comments
- **Dedup cache** — prevents duplicate processing
