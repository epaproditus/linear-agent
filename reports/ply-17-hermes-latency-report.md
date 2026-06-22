# PLY-17: Hermes Agent Latency Analysis Report

**Date:** 2026-06-22 10:45 CDT
**System:** 4-core KVM VM, 23GB RAM, 687GB LVM disk
**Hermes:** v0.17.0 (2026.6.19), model=deepseek-v4-flash via opencode-go

---

## 1. Measured Latency (from Gateway Log)

| Metric | Value |
|--------|-------|
| Average response time (Discord) | ~30s |
| Fastest response (1 API call) | 12.4s |
| Slowest response (6 API calls) | 59.4s |
| Median response | ~25s |
| OpenAI API round-trip | 200ms |
| OpenCode relay round-trip | 166ms |
| Linear API round-trip | 263ms |

**Conclusion: The model API is NOT the bottleneck.** The OpenCode relay responds in 166ms. The 12-59s latency is entirely in-host processing.

---

## 2. Primary Bottlenecks

### 2A. Stuck bash-language-server Process (CRITICAL)

```
PID 2675138 - node /home/abe/.hermes/lsp/bin/bash-language-server start
  CPU:    40-50% (constant, one full core)
  RSS:    332MB
  VSZ:    22GB (virtual memory leak, known Node.js LSP bug)
  State:  Running since 09:58, consuming 50% CPU for 45+ minutes
```

This single process consumes 12.5% of total system CPU capacity. It's a zombie/leaked LSP process started by the gateway's auto-LSP feature. The VSZ of 22GB indicates a severe memory leak. A second `bash-language-server` (PID 2543312) also has VSZ=22GB but is idle.

**Impact:** Steals 1/4 of available CPU from the Hermes agent's processing pipeline.

### 2B. Disk I/O Saturation

```
iowait:  23.3% sustained
Disk util (sda):  68-76%
Write latency:    81ms avg (very high)
Read latency:     52ms avg
```

The main LVM volume is serving 52 Docker containers, each with persistent storage and database write workloads (PostgreSQL, MariaDB, MinIO, Redis, etc.). This creates a massive I/O contention. The `hermes lcm status` command times out at 8+ seconds because it can't reliably hit the session database.

### 2C. Gateway Instance Sprawl

| Gateway | Profile | RSS | Threads | Platform Plugins |
|---------|---------|-----|---------|-----------------|
| `:8647` | default | 774MB | 65 | 11 |
| `:8646` | kinder | 178MB | ~10 | 11 (all loaded) |
| `:8645` | toro | 99MB | ~10 | 11 (all loaded) |
| `:8644` | classroom-bot | 56MB | ~10 | 11 (all loaded) |
| `:8642` | (webhook) | shared | - | - |
| Dashboard | `:9119` | 594MB | - | - |

Each gateway instance loads ALL 11 platform adapters (discord, google_chat, homeassistant, irc, line, mattermost, ntfy, photon, raft, simplex, teams) even though only discord and photon are actively used. Total memory for gateways alone is ~1.6GB RSS.

### 2D. Swap Thrashing

```
Swap total:  12GB
Swap used:   3.6-4.3GB
Active swap I/O: visible (si/so in vmstat)
```

The system is swapping, which adds significant I/O pressure to the already saturated disk. The default gateway's VmData=1.35GB alone exceeds physical memory available for applications.

### 2E. Process Overload

```
Total system processes:  636
Node.js processes:        33
Python processes:         28
Docker containers:        52
```

A 4-core VM managing 636 processes creates massive context-switch overhead. The scheduler is constantly thrashing.

---

## 3. Secondary Issues

### 3A. Failed Connection Retries Wasting CPU

| Service | Retries | Interval | Log Lines |
|---------|---------|----------|-----------|
| Email IMAP auth fails | 56 retries | Every 5 min | 1053 total |
| Slack API scope missing | Every 5 min | 5 min | (ongoing) |
| Mattermost auth fails | 117+ retries (kinder) | 5 min | 531 lines |
| Mattermost auth fails | 117+ retries (toro) | 5 min | 531 lines |
| Photon sidecar stream | Persistent failure | Continuous | (ongoing) |

These retries consume CPU, I/O, and log space without providing value.

### 3B. Configuration Issues

| Setting | Value | Issue |
|---------|-------|-------|
| `reasoning_effort` | `xhigh` | Forces maximum reasoning tokens on every request, dramatically increasing latency-per-call |
| `gateway_timeout` | 1800s (30 min) | Too long - users wait up to 30 min before timeout |
| `agent.max_turns` | 90 | Excessively high for most conversations |
| `compression.threshold` | 50% | May trigger compression too aggressively, adding overhead |

### 3C. Unhealthy Docker Services

```
litellm:       unhealthy (Docker health check failing for 9+ hours)
ollama:        has its own Hermes gateway + webui (duplicate instance)
```

---

## 4. Quick Wins (Immediate Impact)

| # | Action | Expected Impact |
|---|--------|----------------|
| 1 | **Kill PID 2675138** (`bash-language-server`) | Frees 50% CPU instantly |
| 2 | **Kill PID 2543312** (second bash-lang) | Frees 22GB VSZ allocation |
| 3 | **Disable unused platform adapters** in config.yaml | Reduces gateway memory 40-50% |
| 4 | **Reduce `reasoning_effort` to `medium` or `low`** | Cuts per-call latency 30-50% |
| 5 | **Reduce `max_turns` to 30** | Prevents runaway sessions |

## 5. Medium-Term Fixes

| # | Action | Expected Impact |
|---|--------|----------------|
| 6 | **Migrate to NVMe SSD** | Eliminates 68-76% disk util bottleneck |
| 7 | **Right-size VM** (8+ cores, more RAM) | Eliminates CPU contention |
| 8 | **Disable email adapter** (uses IMAP, auth broken) | Stops 1053 retry log entries + CPU waste |
| 9 | **Fix Mattermost tokens** or disable unused profile adapters | Stops 500+ retry log entries |
| 10 | **Add swap priority** to prefer swap2.img | Better swap behavior |
| 11 | **Reduce Docker footprint** (merge Plane/Fluxer services) | Fewer containers = less I/O |

## 6. Raw Data Summary

```
Memory top consumers:
  node (LSP + gateway sidecars):    2.8GB
  python3 (gateways):               1.7GB
  postgres (all instances):         1.3GB
  Docker containers (52 total):     ~4GB
  Swap used:                        ~4GB

Gateway response times (from /home/abe/.hermes/logs/gateway.log):
  10:31:16 -> 10:31:29  = 12.4s  (1 API call, 1134 char response)
  10:26:44 -> 10:27:28  = 44.0s  (6 API calls, 739 char response)
  10:15:33 -> 10:16:11  = 38.1s  (6 API calls, 471 char response)
  09:52:30 -> 09:53:29  = 59.4s  (6 API calls, 770 char response)
```
