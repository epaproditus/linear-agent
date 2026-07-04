# PLY-150 — Virtual workspace investigation

**Issue:** Can Hermes work with a virtual workspace instead of the host? Ideally free.  
**Date:** 2026-07-04  
**Status:** findings + prompt-only task workspace guidance (Option A)

---

## Short answer

**Yes** — Hermes already supports isolated execution via terminal backends. GitHub Actions is also viable, but as a **separate delegation pattern**, not a drop-in Hermes terminal backend.

| Approach | Free? | Isolation | Fits linear-agent today? |
| -------- | ----- | --------- | ------------------------ |
| Hermes Docker backend | Yes (self-hosted) | Container on homelab | Yes — one config line |
| Per-task scratch dir (`~/linear-agent/workspace/{ISSUE}/`) | Yes | Logical separation on host | Yes — prompt guidance (merged) |
| hermes-sandbox | Yes (community) | Ephemeral container per project | Yes — external wrapper |
| GitHub Actions `workflow_dispatch` | Yes (public repos unlimited; 2k min/mo private) | Ephemeral cloud VM | **No native integration** — custom bridge required |

---

## Hermes terminal backends (built-in, free on homelab)

Hermes ships six terminal backends. Three are free with container isolation:

| Backend | Config | Isolation | Overhead |
| ------- | ------ | --------- | -------- |
| **Docker** (recommended) | `terminal.backend: docker` | Full container, persistent across tool calls | ~50ms/cmd |
| **Singularity/Apptainer** | `terminal.backend: singularity` | HPC container | ~100ms/cmd |
| **Local + scratch dirs** | Prompt + `AGENT_WORKDIR` | Directory isolation only | Zero |

Docker is the best default for the homelab setup (52 containers already running). One line in `~/.hermes/config.yaml`:

```yaml
terminal:
  backend: docker
  docker_image: "nikolaik/python-nodejs:python3.11-nodejs20"
  container_persistent: true          # persist across tool calls
  docker_mount_cwd_to_workspace: true
  docker_forward_env:
    - "GITHUB_TOKEN"
    - "LINEAR_API_KEY"
```

Set `container_persistent: false` for fully ephemeral sandboxes that reset after each session.

Paid cloud backends (Modal, Daytona) exist but are not required for this issue.

---

## Per-task scratch workspace (Option A — implemented)

The linear-agent already defines `agent_workdir` at `~/linear-agent/workspace`. Previously the LLM was told the host and default cwd but not instructed to isolate per issue.

**Change:** `format_task_workspace_block()` injects per-issue instructions into both legacy and native mode prompts:

- Work inside `{workdir}/{issue_key}/` (e.g. `~/linear-agent/workspace/PLY-150/`)
- Clone repos there, not elsewhere on the host
- Remove the directory when the issue is resolved

This is prompt-only — Hermes already has `git`, `mkdir`, and `rm -rf` via terminal tools. No lifecycle hook yet (Option B would add automatic create/teardown in the webhook handler).

---

## GitHub Actions — is Claude right?

**Yes, architecturally** — but it is a different model than "virtual workspace for Hermes."

### What GHA gives you

- Genuinely ephemeral cloud VM (ubuntu-latest: 4 vCPU, 16 GB RAM, 14 GB disk)
- **Free and unlimited** on public repos using standard GitHub-hosted runners
- Private repos: 2,000 free minutes/month (GitHub Free)
- Trigger via API from Hermes: `POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches`
- Natural fit for CI-style work: checkout → lint/test/build → push branch → open PR

### What GHA is NOT

GHA is **not** a Hermes terminal backend. Hermes backends are: `local | docker | ssh | daytona | singularity | modal`. There is no `github_actions` backend.

Using GHA means **delegating the entire agent loop** (or a coding sub-task) to a workflow, not running Hermes commands inside Actions transparently.

### What breaks or needs bridging

| linear-agent feature | With GHA delegation |
| -------------------- | ------------------- |
| Live `hermes.tool.progress` on Linear timeline | Lost unless workflow posts activities back via Linear API |
| Hermes session memory / follow-up turns | Separate — workflow is one-shot unless you persist state |
| Homelab MCP servers (n8n, private APIs) | Not reachable unless exposed via secrets/tunnel |
| SSE streaming from Hermes API | Hermes stays on homelab; GHA runs independently |
| Sub-second tool feedback | Minutes of queue + checkout overhead |

### When GHA makes sense

Use GHA as a **cloud box for repo-bound coding tasks**:

1. Linear issue assigned → Hermes reads context on homelab
2. Hermes dispatches `workflow_dispatch` with issue ID, repo, branch name
3. GHA runner checks out repo, runs tests/lint, applies patch, pushes branch, opens PR via `gh`
4. Workflow posts PR URL back (artifact, commit status, or webhook to linear-agent)
5. Hermes links PR on the agent session (`externalUrls`) for Linear Diffs

This mirrors Cursor's background agent pattern more than Hermes-in-a-sandbox.

### When to skip GHA

- Tasks needing live tool progress on the Linear timeline
- Tasks touching homelab services, MCP tools, or private infra
- Conversational follow-ups in the same Hermes session
- Quick investigative work where Docker backend isolation is enough

---

## Recommendation

**Hybrid model** (matches the homelab security posture):

1. **Default:** Hermes Docker terminal backend + per-task scratch dirs under `AGENT_WORKDIR` (free, live SSE, MCP access).
2. **Repo CI tasks:** Optional GHA dispatch for lint/test/build on public target repos — Hermes triggers, GHA executes, PR URL flows back.
3. **Do not** replace the Hermes agent loop with GHA for general Linear issue work — you lose the product's core value (live timeline, session continuity, skills).

### Next steps (not in this PR)

- **Option B:** Lifecycle-managed workspace — webhook handler creates `{workdir}/{issue_key}/` on `created`, deletes on terminal state.
- **GHA bridge (optional):** Hermes skill or `delegate_task` wrapper that calls `gh workflow run` with issue context and polls for PR URL.
- **Hermes config:** Enable `terminal.backend: docker` on the production agent host.

---

## References

- [Hermes terminal backends](https://hermes-agent.nousresearch.com/docs/user-guide/features/terminal-backends)
- [Hermes Docker backend config](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)
- [GitHub Actions billing (public = free)](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [workflow_dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
- linear-agent: `agent_workdir` setting, `format_execution_environment_block()`, `format_task_workspace_block()`
