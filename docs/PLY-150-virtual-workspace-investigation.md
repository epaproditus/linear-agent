# PLY-150 — Virtual workspace investigation

**Issue:** Can Hermes work with a virtual workspace instead of the host? Ideally free.  
**Date:** 2026-07-04  
**Status:** implemented — permanent Hermes, ephemeral per-issue workspace

---

## Goal (clarified)

**Not** ephemeral Hermes (spin up the whole agent in GHA/cloud).  
**Yes** permanent Hermes on the homelab with an **ephemeral workspace per Linear issue**:

```
~/linear-agent/workspace/PLY-150/   ← created when work starts
  └── my-repo/                      ← agent clones here
                                      removed when issue is Done/Canceled
```

Hermes stays put. Only the scratch directory is disposable.

---

## What we implemented

| Layer | Role |
| ----- | ---- |
| **Hermes** (permanent) | Runs on homelab, owns session memory, tools, SSE, skills |
| **linear-agent adapter** | Creates `{AGENT_WORKDIR}/{ISSUE}/` before each turn, tears down on Done/Canceled |
| **Prompt** | Tells Hermes to clone repos and run commands inside that directory only |

### Lifecycle (Option B)

- **`ensure_task_workspace(identifier)`** — called at the start of `_handle_analysis()` (idempotent; follow-ups reuse the same dir)
- **`cleanup_task_workspace(identifier)`** — called when an Issue webhook reports `completed` or `canceled`
- **Safety** — issue keys must match `[A-Za-z0-9_-]+`; cleanup refuses paths outside `AGENT_WORKDIR`

Default base path: `~/linear-agent/workspace` (`AGENT_WORKDIR` in `.env`).

---

## Optional: Hermes Docker terminal backend

For stronger isolation (namespaces, cap-drop) on top of scratch dirs, enable in `~/.hermes/config.yaml`:

```yaml
terminal:
  backend: docker
  docker_image: "nikolaik/python-nodejs:python3.11-nodejs20"
  container_persistent: true
  docker_mount_cwd_to_workspace: true
  docker_forward_env:
    - "GITHUB_TOKEN"
    - "LINEAR_API_KEY"
```

This sandboxes **shell commands**, not the Hermes process itself. Pair with per-issue scratch dirs for defense in depth.

---

## GitHub Actions — wrong tool for this goal

GHA runs an ephemeral **agent loop in the cloud**. That is the opposite of "permanent Hermes, ephemeral workspace."

GHA remains useful as an optional **CI dispatch** (lint/test/build on a target repo), but it does not replace per-issue scratch directories on the homelab.

---

## Recommendation

1. **Shipped:** lifecycle-managed scratch dirs under `AGENT_WORKDIR/{ISSUE}/`
2. **Optional:** Hermes `terminal.backend: docker` for command-level isolation
3. **Skip:** replacing the Hermes loop with GHA for general Linear work

---

## References

- `task_workspace_dir()`, `ensure_task_workspace()`, `cleanup_task_workspace()` in `linear_agent.py`
- [Hermes terminal backends](https://hermes-agent.nousresearch.com/docs/user-guide/features/terminal-backends)
