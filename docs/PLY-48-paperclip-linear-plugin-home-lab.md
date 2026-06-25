# Paperclip Linear Plugin: Home Lab Integration Analysis

PLY-48 — Document how the [Paperclip Linear plugin](https://github.com/Oldharlem/paperclip-linear-plugin) would fit into the home lab setup.

Date: 2026-06-24
Status: documentation

---

## 1. Current Home Lab Landscape

### 1.1 Core Services (Relevant to This Integration)

Service | Runtime | Port | Network | Notes
--------|---------|------|---------|------
Paperclip | PM2 (paperclip) | 3100 | loopback only | Allowed hostname: `paperclip.epaphrodit.us`. No Cloudflare tunnel currently serves it. Embedded PostgreSQL on 54329, local_trusted mode.
Linear Agent | systemd (linear-agent.service) | 8660 | 0.0.0.0 | Custom FastAPI service. Receives Linear webhooks at `webhooks.epaphrodit.us/linear/*`, dispatches to Hermes for autonomous issue handling. Uses Codex CLI as coding agent.
Hermes Agent | systemd (hermes-gateway.service + 4 profile gateways) | internal | messaging-native | Multiple profiles (default, kinder, toro, classroom-bot). Also hermes-webui on 8788, hermes-mcp-sse for LobeChat.
Cloudflare Tunnels | systemd (cloudflared-webhooks.service) | — | epaphrodit.us | Only one tunnel active: webhooks.epaphrodit.us routes /linear/* to :8660, fallback to :8649. Configs exist but are stopped for mattermost, photon, bigcapital, lobe-s3.

### 1.2 Existing Linear Integration (Linear Agent)

The Linear Agent at :8660 is a custom-built autonomous agent:

- Receives Linear webhooks at `webhooks.epaphrodit.us/linear/*`
- HMAC-SHA256 verifies webhook payloads using `LINEAR_WEBHOOK_SECRET`
- Uses `LINEAR_API_KEY` for GraphQL API calls to api.linear.app
- Dispatches work to Hermes Agent for autonomous issue processing
- Runs in a dedicated venv via uvicorn, managed by systemd

### 1.3 Paperclip State

- Healthy on port 3100 (HTTP 200)
- Zero plugins installed (empty `/api/plugins` response)
- Allowed hostname `paperclip.epaphrodit.us` is configured but not reachable from the internet
- `paperclip.epaphrodit.us` DNS resolves to 127.0.0.1 — no Cloudflare tunnel routes it

---

## 2. What the Plugin Provides

The `@oldharlem/paperclip-plugin-linear` plugin creates a bidirectional sync bridge between a Paperclip instance and a Linear workspace:

Direction | Mechanism | What Moves
----------|-----------|------------
Paperclip → Linear | Scheduled jobs (full reconcile + incremental cursor sync) | New/updated Paperclip issues pushed to Linear
Linear → Paperclip | Webhook (signed HMAC-SHA256 deliveries) | Linear issues imported into Paperclip
Bidirectional | Event-driven | Issue comments mirrored both ways

Additionally, the plugin exposes two agent tools so Paperclip agents can interact with Linear during a run: `create-linear-issue` and `search-linear-issues`.

Each Paperclip issue gets a detail tab showing the linked Linear identifier, URL, and last-sync timestamp.

---

## 3. How It Would Fit

### 3.1 Relationship to the Existing Linear Agent

These serve different purposes and can coexist:

Aspect | Linear Agent (existing) | Paperclip Linear Plugin (proposed)
--------|--------------------------|------------------------------------
Purpose | Autonomous AI — Hermes processes Linear issues | Bidirectional issue sync — keeps Paperclip and Linear mirrors aligned
Direction | Linear webhook → Hermes → actions | Paperclip ↔ Linear mirror
Agent Role | Hermes is the worker | Paperclip agents get Linear tool access
Issue Ownership | Linear is source of truth | Paperclip can be source of truth for some workflows

They don't conflict. The Linear Agent handles intelligent autonomous work on Linear issues. The Paperclip plugin syncs issue state between the two systems, allowing Paperclip-managed work to appear in Linear and vice versa.

### 3.2 Potential Workflow Synergies

- A Paperclip agent (backed by Hermes via the hermes-paperclip-adapter) picks up a Paperclip issue and uses `search-linear-issues` to find related Linear tickets before working.
- A Paperclip issue is pushed to Linear for visibility on the team board, while the actual work happens in Paperclip.
- Linear issues are imported into Paperclip for agent processing, and status updates flow back to Linear via the sync.

---

## 4. Deployment Plan

### 4.1 Prerequisites

Item | Status | Action
-----|--------|-------
Paperclip running | Done (PM2, :3100) | —
Linear API key | Exists (Linear Agent .env) | Can reuse or create dedicated key
Linear webhook secret | Exists (Linear Agent .env) | Must create a separate webhook for Paperclip
Cloudflare tunnel for paperclip | Missing | Must create

### 4.2 Step 1: Expose Paperclip via Cloudflare Tunnel

Paperclip needs to be reachable from the internet so Linear can deliver webhooks. Create a dedicated tunnel configuration:

```bash
# Create tunnel config
cat > ~/.cloudflared/config-paperclip.yml << 'EOF'
tunnel: <new-tunnel-id>
credentials-file: /home/abe/.cloudflared/<new-tunnel-id>.json

ingress:
  - hostname: paperclip.epaphrodit.us
    service: http://localhost:3100
  - service: http_status:404
EOF

# Create the tunnel in Cloudflare
cloudflared tunnel create paperclip
cloudflared tunnel route dns paperclip paperclip.epaphrodit.us

# Install and start the service
cloudflared --config ~/.cloudflared/config-paperclip.yml service install
systemctl --user enable --now cloudflared-paperclip.service
```

This is the standard pattern already used for mattermost, photon, bigcapital, and the webhooks tunnel.

### 4.3 Step 2: Install the Plugin into Paperclip

```bash
# Clone the plugin repo
git clone https://github.com/Oldharlem/paperclip-linear-plugin.git ~/paperclip-linear-plugin
cd ~/paperclip-linear-plugin
npm install
npm run build

# Install into the running Paperclip instance
curl -X POST http://127.0.0.1:3100/api/plugins/install \
  -H "Content-Type: application/json" \
  -d '{"packageName":"/home/abe/paperclip-linear-plugin","isLocalPath":true}'
```

Paperclip watches local-path plugins and restarts the worker on rebuild, so updates are frictionless.

### 4.4 Step 3: Configure the Plugin

Store the Linear API key as a Paperclip secret:

```bash
# Add secret via Paperclip's API
curl -X POST http://127.0.0.1:3100/api/secrets \
  -H "Content-Type: application/json" \
  -d '{"name":"linear-api-key","value":"<key>","scope":"plugin:paperclip.linear"}'
```

Configure the plugin settings at `/settings/plugins/paperclip.linear`:

Setting | Value
--------|------
apiKeyRef | `linear-api-key` (secret ref)
webhookSecretRef | `linear-webhook-secret` (secret ref)
apiUrl | `https://api.linear.app/graphql` (default)
pushPaperclipIssues | true (or false, depending on desired direction)
importLinearIssues | true
defaultCompanyId | Paperclip company UUID for imported issues
defaultProjectId | Optional Paperclip project UUID
companyTeamMap | Map Paperclip company UUIDs to Linear team UUIDs
incrementalSyncMinutes | 15 (reasonable polling interval)

Use the "Test Connection" button on the settings page to verify the API key works before saving.

### 4.5 Step 4: Configure the Linear Webhook

In Linear (Settings → API → Webhooks):

1. Create a new webhook
2. URL: `https://paperclip.epaphrodit.us/api/plugins/paperclip.linear/webhooks/linear`
3. Subscribe to `Issue` events
4. Set a signing secret
5. Store the secret name in the plugin's `webhookSecretRef`

### 4.6 Step 5: Verify End-to-End

```bash
# Confirm plugin is loaded
curl -s http://127.0.0.1:3100/api/plugins | python3 -m json.tool

# Test webhook reachability (from outside)
curl -X POST https://paperclip.epaphrodit.us/api/plugins/paperclip.linear/webhooks/linear \
  -H "Content-Type: application/json" \
  -d '{"action":"ping"}'  # Will get a 401 without valid HMAC — that's expected

# Check sync job status in Paperclip dashboard
# Create a test issue in Paperclip and verify it appears in Linear
```

---

## 5. Integration Architecture

```
                          epaphrodit.us (Cloudflare DNS)
                                   │
                    ┌──────────────┼──────────────┐
                    │              │              │
            webhooks.       paperclip.      mattermost.
           epaphrodit.us   epaphrodit.us   epaphrodit.us
                    │              │              │
          ┌─────────┤              │              │
          │         │              │              │
    /linear/*  fallback           │              │
          │         │              │              │
   ┌──────┘    ┌────┘              │              │
   │           │                   │              │
   ▼           ▼                   ▼              ▼
:8660        :8649              :3100          :8065
Linear       Generic            Paperclip      Mattermost
Agent        proxy              (PM2)
(Hermes
backend)

          Paperclip (:3100)
          ┌──────────────────────────────┐
          │                              │
          │  paperclip-linear-plugin     │
          │  ┌──────────────────────┐    │
          │  │ Full sync (cron)     │────┼──► api.linear.app
          │  │ Incremental cursor   │    │    (GraphQL)
          │  │ Webhook handler      │◄───┼─── Linear webhook
          │  │ Agent tools:         │    │    (HMAC-SHA256)
          │  │  - create-linear-issue│   │
          │  │  - search-linear-issues│  │
          │  └──────────────────────┘    │
          │                              │
          │  Paperclip agents ───────────┼──► Hermes Agent
          │  (via hermes-paperclip-      │    (autonomous work)
          │   adapter)                   │
          └──────────────────────────────┘
```

---

## 6. Resource Impact

Resource | Estimate | Notes
---------|----------|------
CPU | Negligible | Plugin is event-driven; sync jobs run on schedule
Memory | ~20–50MB | Node.js worker for the plugin
Disk | ~100MB | Plugin code + npm deps; sync state stored in Paperclip's DB
Network | Minimal | GraphQL calls during sync; webhooks are inbound only
Port | None new | Plugin runs inside Paperclip's existing :3100 process
Tunnel | +1 cloudflared process | ~15MB RAM for the additional tunnel

No new Docker containers needed. The plugin runs as part of Paperclip's existing Node.js process.

---

## 7. Security Considerations

Concern | Mitigation
--------|-----------
Linear API key exposure | Stored in Paperclip's local encrypted secrets, not in plaintext config
Webhook forgery | HMAC-SHA256 verification via `webhookSecretRef`
Paperclip local_trusted mode | Paperclip binds to 127.0.0.1 — only the Cloudflare tunnel can reach it from outside. Localhost access is trusted.
Plugin capabilities | Plugin requests only the capabilities it needs; never requests forbidden capabilities (approval, budget, auth, checkout-override)
Plugin supply chain | Plugin is MIT-licensed, community-maintained, and the code is inspectable. Installing from a local clone (not npm registry) gives full control over the source.

---

## 8. When to Use Each System

Scenario | Use
---------|-----
A Linear issue arrives and needs autonomous AI work | Linear Agent (Hermes processes it)
Paperclip issue should be visible on the Linear board | Paperclip plugin (push)
Linear issues need to be tracked in Paperclip for agent processing | Paperclip plugin (import)
Paperclip agent needs to look up related Linear tickets | Plugin's `search-linear-issues` tool
Paperclip agent needs to file a new Linear ticket | Plugin's `create-linear-issue` tool

The two systems are complementary: the Linear Agent is the "doer" (autonomous AI), while the Paperclip plugin is the "bridge" (state synchronization).

---

## 9. What's Needed to Proceed

Checklist for when this moves from documentation to implementation:

- Create a Cloudflare tunnel for `paperclip.epaphrodit.us` → `localhost:3100`
- Clone and build `paperclip-linear-plugin`
- Create a dedicated Linear API key (or decide to reuse the existing one)
- Create a Linear webhook with signing secret
- Store secrets in Paperclip
- Configure plugin settings (company/team mapping, sync direction, polling interval)
- Test end-to-end: Paperclip issue → Linear, Linear issue → Paperclip
- Monitor sync health via the plugin's dashboard widget

---

## 10. References

- [Paperclip Linear Plugin](https://github.com/Oldharlem/paperclip-linear-plugin) — source and README
- Paperclip skill — installed at `devops/paperclip`, covers PM2 lifecycle, troubleshooting, PostgreSQL corruption recovery
- Linear Agent source — `/home/abe/linear-agent/linear_agent.py`
- Cloudflare tunnel configs — `/home/abe/.cloudflared/config-*.yml`
- Paperclip config — `/home/abe/.paperclip/instances/default/config.json`
