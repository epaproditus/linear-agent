#!/usr/bin/env python3
"""
Plane AI Agent — Hermes-powered autonomous agent for Plane.so.

Integrates with Plane's Agent Run API so you can @-mention this agent
in work items, delegate tasks, and have it respond with typed activities
(thought → action → response), update work items, and add comments.

Architecture
────────────
Plane Webhook POST → HMAC verify → Event router
  → Background asyncio Task
    → Acknowledge (thought activity within a few seconds)
    → Parse context (issue, comments, guidance)
    → Process task (analyze, research, code)
    → Emit action activities for progress
    → Emit response activity with result
    → Update work item (comment, status)

Port: 8648
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode, quote
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic_settings import BaseSettings

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("plane-agent")

# ── Constants ────────────────────────────────────────────────────────────
PLANE_API_URL_DEFAULT = "https://api.plane.so"
WEBHOOK_TIMEOUT_S = 5          # Must HTTP 200 within 5s
KEEPALIVE_INTERVAL_S = 15      # Emit keepalive activity every 15s
PORT = 8648

# ── Hermes response style (shared with linear-agent) ─────────────────────

HERMES_WORK_STYLE = """
Work style:
- Run tools and shell commands yourself — never ask the user to run commands.
- Use the full issue thread and prior comments as context; do not ignore earlier turns.
- When a Hermes skill matches the task, read and follow it completely before improvising.
- Investigate with tools before concluding; the internal draft can be thorough.
- When changing code: smallest correct diff, match existing patterns, no drive-by edits.
- When you modify code in a git repository: use a dedicated branch, commit, push, and open a GitHub pull request (`gh pr create`). Reference the Linear issue identifier in the PR title or body (e.g. "Fixes PLY-43") so Linear auto-links the PR to the issue. Include the PR URL in your final answer.
- Comments only for non-obvious logic; no over-engineering or speculative edge cases.
- Track multi-step work mentally; the session plan checklist in Linear shows progress.
""".strip()

HERMES_REPLY_STYLE = """
Reply style (Plane work-item comment — user already saw tool progress on the timeline):
- Write like a clear technical post: complete sentences, plain language, no jargon padding.
- Match depth to the question — short questions get short answers.
- Open with the finding, answer, or decision — not setup or process narration.
- Reference existing code with citation fences: ```startLine:endLine:filepath.
- For code changes: include the GitHub PR URL in your reply.
- For complex logic or architecture, include a ```mermaid diagram when it clarifies the flow.
- Also use short paragraphs, bullets, or numbered steps where helpful.
- Use markdown sparingly; full URLs for links. Do not over-bold or over-backtick.
- No filler endings ("let me know if…", "happy to help", "say the word").
- Preserve every fact, recommendation, and code change from the draft.
""".strip()

# Agent run plan step statuses
_PLAN_PENDING = "pending"
_PLAN_ACTIVE = "inProgress"
_PLAN_DONE = "completed"
PLAN_STEP_MAX_LEN = 48
PLAN_MAX_STEPS = 5
PLAN_STEP_MAX_WORDS = 6

# Map embedded shell/git commands to short checklist labels.
_PLAN_COMMAND_LABELS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"git\s+status", re.I), "Check git status"),
    (re.compile(r"git\s+log", re.I), "Review recent commits"),
    (re.compile(r"git\s+diff", re.I), "Diff changed files"),
    (re.compile(r"git\s+show", re.I), "Inspect commit"),
    (re.compile(r"git\s+branch", re.I), "List branches"),
    (re.compile(r"git\s+checkout", re.I), "Switch branch"),
    (re.compile(r"git\s+stash", re.I), "Check stash"),
    (re.compile(r"pytest|npm test|cargo test", re.I), "Run tests"),
    (re.compile(r"systemctl|docker compose|docker-compose", re.I), "Check service"),
]

# ── Rate Limiting ──
MAX_CONCURRENT_SESSIONS = 10
RATE_LIMIT_WINDOW_S = 60
RATE_LIMIT_MAX_REQUESTS = 30

# ── Config ───────────────────────────────────────────────────────────────


class Settings(BaseSettings):
    """Environment-based configuration."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    plane_api_key: str = ""
    """Plane bot token (from Bot Token Flow — 24h expiry, auto-refreshable)."""

    plane_webhook_secret: str = ""
    """HMAC signing secret from Plane OAuth app settings."""

    plane_api_url: str = PLANE_API_URL_DEFAULT
    """Base URL for the Plane API server."""

    plane_workspace_slug: str = ""
    """Plane workspace slug (e.g. 'epaphroditus'). Used if not in webhook payload."""

    plane_agent_bot_name: str = "Hermes"
    """How the agent self-identifies in Plane."""

    plane_agent_user_id: str = ""
    """Bot user UUID for self-loop prevention. Auto-detected from app installation if blank."""

    # OAuth credentials for token refresh (bot tokens expire every 24h)
    plane_client_id: str = ""
    """OAuth client ID for bot token refresh."""

    plane_client_secret: str = ""
    """OAuth client secret for bot token refresh."""

    plane_app_installation_id: str = ""
    """Plane app installation UUID for bot token refresh."""

    plane_public_url: str = ""
    """Public base URL for OAuth install (e.g. https://plane.epaphroditus.us)."""

    plane_redirect_uri: str = ""
    """OAuth redirect URI registered in Plane. Defaults to {PLANE_PUBLIC_URL}/oauth/callback."""

    plane_scopes: str = "agents.runs:read agents.runs:write agents.run_activities:read agents.run_activities:write"
    """OAuth scopes to request for agent capabilities. Defaults to all four Agent Run scopes."""

    hermes_api_url: str = "http://127.0.0.1:8642/v1"
    """Hermes API server URL for LLM reasoning."""

    hermes_api_key: str = ""
    """API server key for authentication."""

    hermes_model: str = "hermes-agent"
    """Model name to use via Hermes API."""

    @property
    def effective_redirect_uri(self) -> str:
        if self.plane_redirect_uri:
            return self.plane_redirect_uri
        if self.plane_public_url:
            return f"{self.plane_public_url.rstrip('/')}/plane/oauth/callback"
        return ""

    @property
    def oauth_install_ready(self) -> bool:
        return bool(
            self.plane_client_id
            and self.plane_client_secret
            and self.effective_redirect_uri
        )

    @property
    def configured(self) -> bool:
        return bool(self.plane_webhook_secret) and (
            bool(self.plane_api_key) or self.oauth_install_ready
        )


settings = Settings()

# Safety: don't run without minimal config
assert settings.plane_webhook_secret, (
    "PLANE_WEBHOOK_SECRET must be set. "
    "Copy .env.example to .env and fill in your credentials."
)
assert settings.plane_api_key or settings.oauth_install_ready, (
    "Set PLANE_API_KEY after installation, or configure OAuth install credentials "
    "(PLANE_CLIENT_ID, PLANE_CLIENT_SECRET, and PLANE_PUBLIC_URL or PLANE_REDIRECT_URI) "
    "before installing the app from Plane."
)


# ── Data Models ──────────────────────────────────────────────────────────


class ActivityType(str, Enum):
    thought = "thought"
    elicitation = "elicitation"
    action = "action"
    response = "response"
    error = "error"


class RunAction(str, Enum):
    created = "created"
    prompted = "prompted"


@dataclass
class AgentRun:
    """Represents an active Plane agent run from a webhook event."""

    run_id: str
    work_item_id: str
    work_item_identifier: str
    workspace_id: str
    workspace_slug: str
    project_id: str
    action: RunAction
    body: str = ""           # Current user message
    title: str = ""
    description: str = ""
    priority: int = 0
    labels: list[str] = field(default_factory=list)
    assignee_name: str = ""
    state_name: str = ""
    state_type: str = ""


# ── Plane REST Client ────────────────────────────────────────────────────


class PlaneClient:
    """Async HTTPX client for the Plane REST API.

    Plane uses a RESTful API at ``/api/v1/workspaces/{slug}/...``.
    Auth is via ``Authorization: Bearer ***    Bot tokens expire every 24h and
    are auto-refreshed when ``client_id`` and ``client_secret`` are configured.
    """

    TOKEN_REFRESH_MARGIN_S = 300  # Refresh when 5 min from expiry

    def __init__(
        self,
        api_key: str,
        api_url: str = PLANE_API_URL_DEFAULT,
        *,
        client_id: str = "",
        client_secret: str = "",
        app_installation_id: str = "",
    ) -> None:
        self._api_key = api_key
        self._api_url = api_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._app_installation_id = app_installation_id
        self._client: httpx.AsyncClient | None = None
        self._refresh_lock: asyncio.Lock | None = None

    async def _ensure_token(self) -> str:
        """Return a valid token, refreshing if close to expiry.

        Plane bot tokens expire after 86400s (24h). If OAuth creds
        are configured, we auto-refresh before expiry. Without them,
        the caller must provide a fresh ``api_key``.
        """
        # If no OAuth creds, use the key as-is (no refresh possible)
        if not self._client_id or not self._client_secret or not self._app_installation_id:
            return self._api_key

        # Lazy init the refresh lock
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            # Check if close to expiry — for simplicity we refresh every call
            # since the token endpoint is lightweight. A real implementation
            # would cache the expiry time from the token response.
            try:
                new_token = await self._do_refresh_token()
                if new_token:
                    self._api_key = new_token
            except Exception as e:
                log.warning("Token refresh failed (will use existing key): %s", e)

        return self._api_key

    async def _do_refresh_token(self) -> str | None:
        """POST /auth/o/token/ with client_credentials grant to get a new bot token.

        Uses Basic auth with base64(client_id:client_secret) per Plane docs.
        """
        credentials = f"{self._client_id}:{self._client_secret}"
        encoded = __import__("base64").b64encode(credentials.encode()).decode()

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{self._api_url}/auth/o/token/",
                headers={
                    "Authorization": f"Basic {encoded}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "app_installation_id": self._app_installation_id,
                    "scope": "read write",
                },
            )
            if resp.status_code != 200:
                log.warning(
                    "Token refresh returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
                return None
            data = resp.json()
            new_token = data.get("access_token")
            if new_token:
                log.info("Bot token refreshed successfully (expires_in=%s)",
                         data.get("expires_in", "unknown"))
                return new_token
            return None

    async def _get_client(self) -> httpx.AsyncClient:
        # Ensure token is fresh before creating/reusing client
        token = await self._ensure_token()

        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._api_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "User-Agent": "HermesPlaneAgent/1.0",
                },
                timeout=httpx.Timeout(30.0),
            )
        return self._client

    async def _rest(
        self,
        method: str,
        path: str,
        json_data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        """Make a REST API call.

        Path is relative to ``/api/v1/workspaces/{slug}/``.
        Returns the parsed JSON body. Raises on non-2xx.
        """
        # Ensure fresh token before each API call
        token = await self._ensure_token()
        client = await self._get_client()
        # Update the auth header in case token was refreshed
        client.headers["Authorization"] = f"Bearer {token}"
        url = f"/api/v1/{path.lstrip('/')}"
        resp = await client.request(
            method=method,
            url=url,
            json=json_data,
            params=params,
        )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise RuntimeError(
                f"Plane API {method} {url} returned {resp.status_code}: {detail}"
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    def update_credentials(
        self,
        *,
        api_key: str | None = None,
        app_installation_id: str | None = None,
    ) -> None:
        """Update runtime credentials after OAuth installation."""
        if api_key:
            self._api_key = api_key
        if app_installation_id:
            self._app_installation_id = app_installation_id
        if self._client and not self._client.is_closed:
            self._client.headers["Authorization"] = f"Bearer {self._api_key}"

    # ── Agent Run Activities ──

    async def create_activity(
        self,
        workspace_slug: str,
        run_id: str,
        activity_type: ActivityType,
        body: str,
        *,
        action_label: str | None = None,
        action_param: str | None = None,
        action_result: str | None = None,
        ephemeral: bool = False,
        signal: str | None = None,
        signal_metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Emit an agent run activity (thought/action/response/error)."""
        content: dict[str, Any] = {"type": activity_type.value}

        if activity_type == ActivityType.action:
            content["action"] = action_label or ""
            content["body"] = body
            # Plane expects `parameters` as a key-value object (not a string)
            params: dict[str, str] = {}
            if action_param:
                params["query"] = action_param
            if action_result:
                params["result"] = action_result
            if params:
                content["parameters"] = params
        else:
            content["body"] = body

        activity: dict[str, Any] = {
            "type": activity_type.value,
            "content": content,
            "ephemeral": ephemeral,
        }
        if signal:
            activity["signal"] = signal
        if signal_metadata:
            activity["signal_metadata"] = signal_metadata

        try:
            path = f"workspaces/{workspace_slug}/agent-runs/{run_id}/activities/"
            await self._rest("POST", path, json_data=activity)
            return True
        except RuntimeError as e:
            log.warning("create_activity failed: %s", e)
            return False

    async def acknowledge(
        self, workspace_slug: str, run_id: str, message: str = ""
    ) -> bool:
        """Send 'thought' activity quickly to show the agent is working."""
        return await self.create_activity(
            workspace_slug,
            run_id,
            ActivityType.thought,
            body=message or "Hermes agent here. Processing the request...",
            ephemeral=True,
        )

    async def send_action(
        self,
        workspace_slug: str,
        run_id: str,
        label: str,
        param: str,
        body: str = "",
        result: str | None = None,
        ephemeral: bool = True,
    ) -> bool:
        """Emit an 'action' activity."""
        return await self.create_activity(
            workspace_slug,
            run_id,
            ActivityType.action,
            body=body or f"**{label}**...",
            action_label=label,
            action_param=param,
            action_result=result,
            ephemeral=ephemeral,
        )

    async def send_response(
        self, workspace_slug: str, run_id: str, body: str
    ) -> bool:
        """Emit the final 'response' activity (non-ephemeral, visible as comment)."""
        result = await self.create_activity(
            workspace_slug, run_id, ActivityType.response, body=body,
        )
        log.info(
            "send_response(%s): success=%s response_len=%d",
            run_id[:8], result, len(body),
        )
        return result

    async def send_error(
        self, workspace_slug: str, run_id: str, body: str
    ) -> bool:
        """Emit an 'error' activity."""
        result = await self.create_activity(
            workspace_slug, run_id, ActivityType.error, body=body,
        )
        log.warning(
            "send_error(%s): success=%s error_len=%d",
            run_id[:8], result, len(body),
        )
        return result

    async def get_activities(
        self, workspace_slug: str, run_id: str
    ) -> list[dict[str, Any]]:
        """Fetch all activities for an agent run."""
        try:
            path = f"workspaces/{workspace_slug}/agent-runs/{run_id}/activities/"
            data = await self._rest("GET", path)
            if isinstance(data, dict):
                return data.get("results", data.get("data", []))
            if isinstance(data, list):
                return data
            return []
        except RuntimeError:
            return []

    # ── Work Items (Issues) ──

    async def get_work_item(
        self, workspace_slug: str, work_item_id: str
    ) -> dict[str, Any] | None:
        """Fetch a single work item by UUID or identifier."""
        try:
            path = f"workspaces/{workspace_slug}/work-items/{work_item_id}/"
            data = await self._rest("GET", path)
            return data if isinstance(data, dict) else None
        except RuntimeError:
            return None

    async def update_work_item(
        self, workspace_slug: str, work_item_id: str, **kwargs: Any
    ) -> bool:
        """Update work item fields (state, assignee, priority, etc.)."""
        try:
            path = f"workspaces/{workspace_slug}/work-items/{work_item_id}/"
            await self._rest("PATCH", path, json_data=kwargs)
            return True
        except RuntimeError as e:
            log.warning("update_work_item failed: %s", e)
            return False

    async def comment_on_work_item(
        self, workspace_slug: str, work_item_id: str, body: str,
        parent_id: str | None = None,
    ) -> bool:
        """Add a comment to a work item."""
        data: dict[str, Any] = {"body": body}
        if parent_id:
            data["parent"] = parent_id
        try:
            path = f"workspaces/{workspace_slug}/work-items/{work_item_id}/comments/"
            await self._rest("POST", path, json_data=data)
            return True
        except RuntimeError as e:
            log.warning("comment_on_work_item failed: %s", e)
            return False

    # ── Workspace info (for resolving slug) ──

    async def get_workspace(self, workspace_id: str) -> dict[str, Any] | None:
        """Fetch workspace details by ID."""
        try:
            data = await self._rest("GET", f"workspaces/{workspace_id}/")
            return data if isinstance(data, dict) else None
        except RuntimeError:
            return None

    async def get_workspace_slug_from_installation(self) -> str | None:
        """Resolve workspace slug via OAuth app installation endpoint.

        Uses ``GET /auth/o/app-installation/?id=APP_INSTALLATION_ID`` per
        Plane's Bot Token Flow documentation. Returns the workspace slug
        and also sets the bot user ID on the settings for self-loop prevention.
        """
        if not self._app_installation_id:
            return None

        token = await self._ensure_token()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{self._api_url}/auth/o/app-installation/",
                    params={"id": self._app_installation_id},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code != 200:
                    log.warning("App installation query returned %d", resp.status_code)
                    return None
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    installation = data[0]
                    ws_detail = installation.get("workspace_detail", {})
                    slug = ws_detail.get("slug", "")
                    # Also capture the bot user ID for self-loop prevention
                    bot_user_id = installation.get("app_bot", "")
                    if bot_user_id and not settings.plane_agent_user_id:
                        log.info("Auto-detected bot user ID: %s", bot_user_id[:8])
                    return slug or None
                return None
        except Exception as e:
            log.warning("Failed to resolve workspace slug from installation: %s", e)
            return None


# ── Webhook Security ─────────────────────────────────────────────────────


def verify_hmac(payload: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 verification with timing-safe comparison.

    Uses the official Plane webhook verification pattern:
    ``hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()``

    Plane sends the signature in the ``X-Plane-Signature`` header as a
    raw hex HMAC-SHA256 digest (no prefix).
    """
    expected = hmac.new(
        secret.encode(), payload, "sha256"
    ).hexdigest()
    result = hmac.compare_digest(expected, signature)
    if not result:
        log.warning(
            "HMAC mismatch: computed_hash=%s... received_hash=%s... "
            "payload_len=%d secret_len=%d",
            expected[:16], str(signature)[:16], len(payload), len(secret),
        )
    return result


# ── OAuth install flow ───────────────────────────────────────────────────


def build_plane_consent_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: str = "",
    api_url: str = PLANE_API_URL_DEFAULT,
) -> str:
    """Build Plane's OAuth consent URL for app installation."""
    params: dict[str, str] = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
    }
    if scopes:
        params["scope"] = scopes
    return f"{api_url.rstrip('/')}/auth/o/authorize-app/?{urlencode(params)}"


async def exchange_bot_token(
    *,
    client_id: str,
    client_secret: str,
    app_installation_id: str,
    scopes: str = "",
    api_url: str = PLANE_API_URL_DEFAULT,
) -> dict[str, Any]:
    """Exchange an app installation ID for a bot token (client credentials flow)."""
    credentials = f"{client_id}:{client_secret}"
    encoded = base64.b64encode(credentials.encode()).decode()

    data: dict[str, str] = {
        "grant_type": "client_credentials",
        "app_installation_id": app_installation_id,
    }
    if scopes:
        data["scope"] = scopes

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{api_url.rstrip('/')}/auth/o/token/",
            headers={
                "Authorization": f"Basic {encoded}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=data,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Bot token exchange failed ({resp.status_code}): {resp.text[:300]}"
            )
        data = resp.json()
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError("Bot token exchange returned no access_token")
        return data


async def fetch_app_installation(
    *,
    bot_token: str,
    app_installation_id: str,
    api_url: str = PLANE_API_URL_DEFAULT,
) -> dict[str, Any] | None:
    """Fetch app installation details (workspace slug, bot user ID)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{api_url.rstrip('/')}/auth/o/app-installation/",
            params={"id": app_installation_id},
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            log.warning("App installation query returned %d", resp.status_code)
            return None
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None


def update_env_file(
    env_path: Path,
    updates: dict[str, str],
) -> None:
    """Upsert key=value pairs in a .env file."""
    lines: list[str] = []
    seen: set[str] = set()

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    for key, value in updates.items():
        replacement = f"{key}={value}"
        replaced = False
        for idx, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[idx] = replacement
                replaced = True
                break
        if not replaced:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(replacement)
        seen.add(key)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Progress helpers ─────────────────────────────────────────────────────


def _normalize_progress_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _shorten_plan_step(
    text: str,
    max_len: int = PLAN_STEP_MAX_LEN,
    max_words: int = PLAN_STEP_MAX_WORDS,
) -> str:
    """Compress a plan step to a short checklist label."""
    t = _normalize_progress_markdown(text.strip())
    t = re.sub(
        r"^(?:i will |i'll |we need to |need to |first,? |then,? |next,? )+",
        "", t, flags=re.IGNORECASE,
    )
    t = re.sub(r"^(?:read the |check the |review the |search for the )", "", t, flags=re.IGNORECASE)
    t = re.sub(
        r"\b(?:carefully|thoroughly|completely|in order to|so that|in order)\b",
        "", t, flags=re.IGNORECASE,
    )
    t = re.sub(r"\s+", " ", t).strip(" ,;.-")
    if not t:
        return ""
    words = t.split()
    if len(words) > max_words:
        t = " ".join(words[:max_words])
    if len(t) <= max_len:
        return t[0].upper() + t[1:] if t else t
    cut = t[: max_len - 1].rsplit(" ", 1)[0]
    return (cut or t[: max_len - 1]) + "\u2026"


def _parse_hermes_plan_json(text: str) -> list[str]:
    """Extract plan step strings from Hermes planning response JSON."""
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [str(s).strip() for s in data if str(s).strip()]
    if isinstance(data, dict):
        for key in ("steps", "plan", "tasks"):
            items = data.get(key)
            if isinstance(items, list):
                out: list[str] = []
                for item in items:
                    if isinstance(item, str):
                        out.append(item.strip())
                    elif isinstance(item, dict):
                        label = item.get("content") or item.get("step") or item.get("title")
                        if label:
                            out.append(str(label).strip())
                return [s for s in out if s]
    return []


# ── Discovery Tracker (reused from linear-agent pattern) ─────────────────


@dataclass
class DiscoveryTracker:
    """Tracks and emits discovery activities during work item processing.

    Fire-and-forget: failures during emission are logged but never block.
    """

    workspace_slug: str
    run_id: str
    plane: PlaneClient
    last_emit: float = 0.0
    activity_count: int = 0
    _keepalive_ctx: str = ""
    _pending_tasks: list[asyncio.Task] = field(default_factory=list)
    _skip_texts: set = field(default_factory=set)
    tool_progress: list[str] = field(default_factory=list)

    MIN_ACTIVITY_INTERVAL: float = 0.8
    MILESTONE_INTERVAL: float = 1.5

    async def found(self, detail: str) -> bool:
        return await self.progress(detail)

    async def identified(self, detail: str) -> bool:
        return await self.progress(detail)

    async def decided(self, detail: str) -> bool:
        return await self.progress(detail)

    async def created(self, detail: str) -> bool:
        return await self.progress(detail)

    async def verified(self, detail: str) -> bool:
        return await self.progress(detail)

    async def in_progress(self, description: str) -> bool:
        self._keepalive_ctx = description
        self._skip_texts.add(description)
        return await self._emit("", description, ephemeral=True)

    async def progress(self, detail: str) -> bool:
        return await self._emit("", detail, ephemeral=False)

    async def _emit(self, kind: str, detail: str, ephemeral: bool) -> bool:
        now = time.monotonic()
        interval = 0.5 if ephemeral else self.MILESTONE_INTERVAL
        if now - self.last_emit < interval:
            return False
        self.last_emit = now
        self.activity_count += 1
        task = asyncio.create_task(self._do_emit(detail[:500], ephemeral))
        self._pending_tasks.append(task)
        self._pending_tasks = [t for t in self._pending_tasks if not t.done()]
        return True

    async def _do_emit(self, detail: str, ephemeral: bool) -> None:
        try:
            await self.plane.create_activity(
                self.workspace_slug,
                self.run_id,
                ActivityType.thought,
                body=detail,
                ephemeral=ephemeral,
            )
        except Exception:
            log.warning("DiscoveryTracker: failed to emit: %s", detail[:60])

    async def flush(self) -> None:
        tasks = self._pending_tasks[:]
        self._pending_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def keepalive_context(self) -> str:
        if self._keepalive_ctx:
            return self._keepalive_ctx[0].upper() + self._keepalive_ctx[1:]
        return "Working on it..."

    @property
    def ws(self) -> str:
        return self.workspace_slug


@dataclass
class RunPlanTracker:
    """Plan steps for a Plane agent run — synced to Hermes."""

    run_id: str
    plane: PlaneClient
    workspace_slug: str
    steps: list[dict[str, str]] = field(default_factory=list)

    async def start_fallback(self, identifier: str) -> None:
        self.steps = [
            {"content": f"Examine {identifier}", "status": _PLAN_DONE},
            {"content": "Investigate with tools", "status": _PLAN_ACTIVE},
            {"content": "Deliver final answer", "status": _PLAN_PENDING},
        ]

    async def set_from_hermes(self, step_texts: list[str]) -> None:
        texts = [_shorten_plan_step(t) for t in step_texts if t.strip()]
        texts = [t for t in texts if t][:PLAN_MAX_STEPS]
        if len(texts) < 2:
            return
        self.steps = [
            {"content": text, "status": _PLAN_PENDING} for text in texts
        ]
        self.steps[0]["status"] = _PLAN_ACTIVE

    async def advance(self) -> None:
        for i, step in enumerate(self.steps):
            if step["status"] != _PLAN_ACTIVE:
                continue
            step["status"] = _PLAN_DONE
            if i + 1 < len(self.steps):
                self.steps[i + 1]["status"] = _PLAN_ACTIVE
            return

    async def finish(self) -> None:
        self.steps = [
            {**step, "status": _PLAN_DONE} for step in self.steps
        ]


class ProgressQueueWorker:
    """Dedicated background worker that drains a queue of progress text
    and POSTs each item to Plane via ``tracker.progress()``."""

    def __init__(self, tracker: DiscoveryTracker | None) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tracker = tracker
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def put(self, text: str) -> None:
        await self._queue.put(text)

    async def drain_and_stop(self) -> None:
        await self._queue.join()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        last_emit = 0.0
        try:
            while True:
                text = await self._queue.get()
                if self._tracker:
                    now = time.monotonic()
                    if now - last_emit >= self._tracker.MILESTONE_INTERVAL:
                        last_emit = now
                        try:
                            await self._tracker._do_emit(text[:500], ephemeral=False)
                        except Exception:
                            log.warning("ProgressQueueWorker: _do_emit failed", exc_info=True)
                self._queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.error("ProgressQueueWorker: crashed", exc_info=True)
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()


# ── Hermes Tool Progress → Discovery Text ─────────────────────────
# (Minimal subset from linear-agent — enough for core operations)

_HERMES_TOOL_VERBS: dict[str, str] = {
    "read_file": "Reading",
    "write_file": "Writing",
    "search_files": "Searching",
    "web_search": "Searching web",
    "web_extract": "Fetching",
    "terminal": "Running",
    "execute_code": "Running code",
    "browser": "Browsing",
}

_NOISE_LABELS = frozenset({
    "fetching", "searching web", "searching", "browsing", "running",
    "hermes tool progress", "working on it",
})


def _is_noise_label(label: str) -> bool:
    lower = label.lower().strip()
    if lower in _NOISE_LABELS:
        return True
    if lower in {v.lower() for v in _HERMES_TOOL_VERBS.values()}:
        return True
    if lower in _HERMES_TOOL_VERBS:
        return True
    return lower in ("log", "*.log")


def _is_url(text: str) -> bool:
    t = text.strip().lower()
    return t.startswith("http://") or t.startswith("https://")


_GITHUB_PR_URL_RE = re.compile(
    r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)", re.I,
)


def extract_github_pr_urls(*texts: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in _GITHUB_PR_URL_RE.finditer(text):
            url = match.group(0).rstrip(").,]>")
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _summarize_url(url: str) -> str:
    lower = url.lower()
    if "github.com" in lower:
        issue = re.search(r"/issues/(\d+)", url)
        if issue:
            return f"Checked GitHub issue #{issue.group(1)}"
        pr = re.search(r"/pull/(\d+)", url)
        if pr:
            return f"Reviewed GitHub PR #{pr.group(1)}"
        return "Opened GitHub link"
    return "Fetched web page"


def _base_name(path: str) -> str:
    return os.path.basename(path.rstrip("/"))


def format_hermes_tool_progress(event: dict) -> str | None:
    """Convert a hermes.tool.progress SSE payload to timeline text."""
    tool = (event.get("tool") or event.get("name") or "").strip()
    if not tool or tool.startswith("_"):
        return None

    status = (event.get("status") or "running").lower()
    if status not in ("running", "completed", ""):
        return None

    label = (event.get("label") or "").strip()
    if _is_noise_label(label):
        return None
    if _is_url(label):
        return _summarize_url(label)
    if label and len(label) <= 80:
        return label

    # Fallback: use tool name
    verb = _HERMES_TOOL_VERBS.get(tool, tool.replace("_", " "))
    args_raw = event.get("input") or event.get("args") or {}
    if isinstance(args_raw, str):
        try:
            args_raw = json.loads(args_raw)
        except json.JSONDecodeError:
            args_raw = {}
    args: dict = args_raw if isinstance(args_raw, dict) else {}

    if tool == "terminal":
        cmd = args.get("command", "")
        if cmd:
            return f"Ran `{str(cmd)[:72]}`"
    if tool == "read_file":
        path = args.get("path", "")
        if path:
            return f"Read `{_base_name(str(path))}`"
    if tool == "web_search":
        query = args.get("query", "")
        if query:
            return f"Searched web for `{str(query)[:60]}`"

    return verb.capitalize() if verb else None


# ── Task Processor ──────────────────────────────────────────────────────


class TaskProcessor:
    """Processes an agent run — analyzes the work item, takes action."""

    def __init__(self, plane: PlaneClient) -> None:
        self.plane = plane

    async def _link_prs_to_run(
        self,
        workspace_slug: str,
        run_id: str,
        response_text: str,
    ) -> list[str]:
        pr_urls = extract_github_pr_urls(response_text)
        if not pr_urls:
            return []
        log.info("Found PR URLs in response: %s", pr_urls)
        return pr_urls

    async def process(
        self, run: AgentRun, work_item: dict[str, Any] | None
    ) -> None:
        """Main processing pipeline for an agent run."""
        run_id = run.run_id
        ws = run.workspace_slug
        log.info(
            "Processing run=%s work_item=%s action=%s",
            run_id, run.work_item_identifier, run.action.value,
        )

        try:
            # 1. Fetch full work item if we only have partial data
            if work_item is None:
                work_item = await self.plane.get_work_item(ws, run.work_item_id)

            if not work_item:
                await self.plane.send_error(
                    ws, run_id,
                    f"Could not fetch work item {run.work_item_identifier}.",
                )
                return

            title = work_item.get("name", work_item.get("title", run.title))
            description = work_item.get("description_html", "") or work_item.get("description", "") or run.description
            log.info("Work item: %s — %s", run.work_item_identifier, title)

            # 2. Acknowledge quickly with thought activity
            await self.plane.acknowledge(
                ws, run_id,
                f"Received your request about **{run.work_item_identifier}** — investigating...",
            )

            # 3. Always route to Hermes LLM reasoning
            log.info("Routing to _handle_analysis")
            await self._handle_analysis(run, work_item, run_id, ws)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Error processing run %s", run_id)
            await self.plane.send_error(
                ws, run_id,
                f"An error occurred while processing this request:\n```\n{e}\n```",
            )

    def build_llm_prompt(
        self,
        run: AgentRun,
        work_item: dict[str, Any],
        user_request: str,
        plan_steps: list[str] | None = None,
    ) -> str:
        """Assemble the full LLM prompt from work item context."""
        identifier = work_item.get("identifier", work_item.get("id", run.work_item_identifier))[:20]
        title = work_item.get("name", work_item.get("title", run.title))
        description = work_item.get("description_html", "") or work_item.get("description", "") or run.description

        # Extract label names from the work item
        labels_raw = work_item.get("labels", []) or []
        label_names = [l.get("name", str(l)) if isinstance(l, dict) else str(l) for l in labels_raw]

        # Extract state info
        state = work_item.get("state", {}) or work_item.get("state_name", "")
        state_name = state.get("name", state) if isinstance(state, dict) else state

        # Extract project
        project = work_item.get("project", {}) or {}
        project_name = project.get("name", "") if isinstance(project, dict) else str(project)

        plan_block = ""
        if plan_steps:
            plan_block = (
                "\nYour plan for this issue (follow in order):\n"
                + "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan_steps))
                + "\n"
            )

        return (
            f"You are Hermes, an autonomous agent working inside Plane."
            f" The Hermes API server runs tools (filesystem, shell, web"
            f" search) on your behalf during each request.\n"
            f"\n"
            f"You can access Plane's REST API using the bot token available"
            f" as the environment variable $PLANE_API_KEY"
            f" \u2014 accessible via 'echo $PLANE_API_KEY' in your shell"
            f" tools if you need it.\n"
            f"\n"
            f"Work Item: {identifier} \u2014 {title}"
            f" | Status: {state_name}\n"
            f"Project: {project_name or '(none)'}\n"
            f"Labels: {', '.join(label_names) or 'none'}\n"
            f"Description: {description or '(no description)'}\n"
            f"{plan_block}"
            f"\n"
            f"Investigate with your tools, then produce a thorough internal"
            f" draft. Tool actions appear on the timeline as they"
            f" run; a separate pass will write the user-facing reply.\n"
            f"\n"
            f"{HERMES_WORK_STYLE}\n"
            f"\n"
            f"User: {user_request}\n"
            f"\n"
            f"Do what needs to be done. Use your tools and report what you"
            f" actually did and found. If it's casual or needs discussion,"
            f" just reply naturally. Do not ask for confirmation before"
            f" starting. Be concise. Do not introduce yourself or list"
            f" capabilities."
        )

    def _build_plan_prompt(
        self,
        identifier: str,
        title: str,
        user_request: str,
        description: str,
    ) -> str:
        desc = (description or "")[:800]
        return (
            "You are Hermes, planning how to handle a Plane work item.\n"
            "Do not use tools. Output a JSON plan only.\n"
            "\n"
            f"Work Item: {identifier} \u2014 {title}\n"
            f"Description: {desc or '(none)'}\n"
            f"User request:\n{user_request}\n"
            "\n"
            "Return 3\u20135 terse checklist steps.\n"
            "Each step: max 6 words, imperative, intent only \u2014 not how.\n"
            "No explanations, sub-clauses, or 'in order to' phrasing.\n"
            "Do not include 'write final answer' \u2014 that is implicit.\n"
            "\n"
            'Respond with JSON only: {"steps": ["...", "..."]}'
        )

    async def _call_llm_plan(
        self,
        identifier: str,
        title: str,
        user_request: str,
        description: str,
    ) -> list[str]:
        """Ask Hermes for run-specific plan steps before investigation."""
        if not settings.hermes_api_key:
            return []

        prompt = self._build_plan_prompt(
            identifier, title, user_request, description,
        )
        headers = {
            "Authorization": f"Bearer {settings.hermes_api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{settings.hermes_api_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": settings.hermes_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.2,
                        "max_tokens": 256,
                        "stream": False,
                        "stream_tool_progress": False,
                    },
                )
                if resp.status_code != 200:
                    log.warning("Plan call: Hermes API returned %d", resp.status_code)
                    return []
                data = resp.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                steps = [_shorten_plan_step(s) for s in _parse_hermes_plan_json(content or "")]
                steps = [s for s in steps if s][:PLAN_MAX_STEPS]
                if len(steps) >= 2:
                    log.info("Hermes plan: %d steps", len(steps))
                    return steps
                return []
        except Exception:
            log.warning("Plan call failed", exc_info=True)
            return []

    async def _handle_analysis(
        self,
        run: AgentRun,
        work_item: dict[str, Any],
        run_id: str,
        ws: str,
    ) -> None:
        """Non-coding task: use LLM to reason and respond."""
        identifier = work_item.get("identifier", work_item.get("id", run.work_item_identifier))[:20]
        title = work_item.get("name", work_item.get("title", run.title))
        description = (work_item.get("description_html", "") or
                       work_item.get("description", "") or run.description)
        description_text = re.sub(r"<[^>]+>", "", description)  # Strip HTML if present

        # User request
        user_request = run.body or description_text or f"Respond to work item {identifier}"

        # Plan
        plan = RunPlanTracker(run_id=run_id, plane=self.plane, workspace_slug=ws)
        hermes_steps = await self._call_llm_plan(
            identifier=identifier,
            title=title,
            user_request=user_request,
            description=description_text,
        )
        if hermes_steps:
            await plan.set_from_hermes(hermes_steps)
        else:
            await plan.start_fallback(identifier)

        prompt = self.build_llm_prompt(
            run, work_item, user_request,
            plan_steps=hermes_steps or [s["content"] for s in plan.steps],
        )

        # DiscoveryTracker for progress updates
        tracker = DiscoveryTracker(
            workspace_slug=ws, run_id=run_id, plane=self.plane,
        )
        await tracker.in_progress(f"Examining {identifier}")

        # Background keepalive
        keepalive_task = asyncio.create_task(
            self._keep_run_alive(run_id, ws, tracker)
        )

        try:
            draft_text = await self._call_llm(
                prompt, tracker, plan=plan,
            )
        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

        if draft_text:
            if tracker:
                await tracker.flush()

            response_text = draft_text
            if tracker and tracker.tool_progress:
                finalized = await self._call_llm_finalize(
                    draft=draft_text,
                    user_request=user_request,
                    tool_progress=tracker.tool_progress,
                )
                if finalized:
                    response_text = finalized
                    log.info(
                        "Phase-2 summary: draft_len=%d final_len=%d tool_steps=%d",
                        len(draft_text), len(response_text), len(tracker.tool_progress),
                    )

            pr_urls = await self._link_prs_to_run(ws, run_id, response_text)
            if pr_urls and not any(u in response_text for u in pr_urls):
                heading = "Pull requests" if len(pr_urls) > 1 else "Pull request"
                response_text = f"{response_text.rstrip()}\n\n**{heading}:**\n" + "\n".join(f"- {url}" for url in pr_urls)

            await plan.finish()
            await self.plane.send_response(ws, run_id, response_text)
        else:
            await self.plane.send_response(
                ws, run_id,
                f"Could not generate a response.",
            )

    async def _call_llm(
        self, prompt: str,
        tracker: DiscoveryTracker | None = None,
        plan: RunPlanTracker | None = None,
    ) -> str | None:
        """Call Hermes API server for reasoning with streaming support."""
        if not settings.hermes_api_key:
            log.warning("HERMES_API_KEY not set \u2014 cannot call LLM")
            return None

        last_drought_time = 0.0

        for attempt in range(2):
            start = time.monotonic()
            progress_worker = ProgressQueueWorker(tracker) if tracker else None
            if progress_worker:
                progress_worker.start()
            try:
                accumulated = ""
                headers = {
                    "Authorization": f"Bearer {settings.hermes_api_key}",
                    "Content-Type": "application/json",
                }

                async with httpx.AsyncClient(timeout=600.0) as client:
                    async with client.stream(
                        "POST",
                        f"{settings.hermes_api_url}/chat/completions",
                        headers=headers,
                        json={
                            "model": settings.hermes_model,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.7,
                            "max_tokens": 1000,
                            "stream": True,
                            "stream_tool_progress": True,
                        },
                    ) as resp:
                        if resp.status_code != 200:
                            elapsed = time.monotonic() - start
                            log.warning(
                                "Hermes API returned %d (attempt %d/2)",
                                resp.status_code, attempt + 1,
                            )
                            if attempt == 0 and resp.status_code >= 500:
                                await asyncio.sleep(5)
                                continue
                            return None

                        current_event = ""
                        seen_progress: set[str] = set()

                        async for line in resp.aiter_lines():
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("event:"):
                                current_event = line[6:].strip()
                                continue
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload == "[DONE]":
                                break

                            if current_event == "hermes.tool.progress":
                                try:
                                    progress_data = json.loads(payload)
                                except json.JSONDecodeError:
                                    current_event = ""
                                    continue
                                discovery = format_hermes_tool_progress(progress_data)
                                if (
                                    discovery
                                    and tracker
                                    and progress_worker
                                    and discovery not in seen_progress
                                ):
                                    seen_progress.add(discovery)
                                    tracker._keepalive_ctx = discovery[:100]
                                    tracker._skip_texts.add(discovery)
                                    tracker.tool_progress.append(discovery)
                                    await progress_worker.put(discovery)
                                    if plan:
                                        await plan.advance()
                                    log.info("Hermes tool progress: %s", discovery[:80])
                                current_event = ""
                                continue

                            try:
                                chunk = json.loads(payload)
                            except json.JSONDecodeError:
                                continue

                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                accumulated += content

                            now = time.monotonic()
                            if (
                                not content
                                and now - last_drought_time > 5.0
                                and tracker
                                and progress_worker
                            ):
                                ctx = tracker.keepalive_context()
                                if ctx and ctx not in tracker._skip_texts:
                                    await progress_worker.put(ctx[:200])
                                last_drought_time = now

                            current_event = ""

                elapsed = time.monotonic() - start
                result = accumulated.strip() if accumulated else None
                log.info(
                    "Hermes API call succeeded: response_len=%d elapsed=%.2fs (attempt %d/2)",
                    len(result or ""), elapsed, attempt + 1,
                )
                if progress_worker:
                    await progress_worker.drain_and_stop()
                return result

            except httpx.TimeoutException:
                elapsed = time.monotonic() - start
                log.warning("Hermes API call timed out after %.2fs (attempt %d/2)", elapsed, attempt + 1)
                if attempt == 0:
                    if progress_worker:
                        await progress_worker.drain_and_stop()
                    await asyncio.sleep(5)
                    continue
                if progress_worker:
                    await progress_worker.drain_and_stop()
                return None
            except Exception as e:
                elapsed = time.monotonic() - start
                log.warning("Hermes API call failed after %.2fs (attempt %d/2): %s", elapsed, attempt + 1, e)
                if attempt == 0:
                    if progress_worker:
                        await progress_worker.drain_and_stop()
                    await asyncio.sleep(5)
                    continue
                if progress_worker:
                    await progress_worker.drain_and_stop()
                return None

        if progress_worker:
            await progress_worker.drain_and_stop()
        return None

    def _build_finalize_prompt(
        self,
        draft: str,
        user_request: str,
        tool_progress: list[str],
    ) -> str:
        timeline = "\n".join(f"- {line}" for line in tool_progress[:25])
        return (
            "You are Hermes, writing the final reply on a Plane work item.\n"
            "\n"
            "The investigation is complete. Tool actions below were already"
            " shown live on the timeline. The user read them while you"
            " worked. Your job now is ONLY the final written answer.\n"
            "\n"
            f"Timeline already shown to the user:\n{timeline}\n"
            "\n"
            f"User question:\n{user_request}\n"
            "\n"
            "Internal draft (may include process narration mixed with findings):\n"
            f"---\n{draft}\n"
            "---\n"
            "\n"
            "Write the final user-facing reply.\n"
            "- Conclusions, findings, decisions, and direct answers only.\n"
            "- No process narration: do not describe steps taken, planned,"
            " or about to be taken.\n"
            "- Do not repeat or summarize tool actions from the timeline.\n"
            "- Do not use tools. Text output only.\n"
            "- If the draft is already a clean direct answer, return it trimmed.\n"
            f"\n"
            f"{HERMES_REPLY_STYLE}"
        )

    async def _call_llm_finalize(
        self,
        draft: str,
        user_request: str,
        tool_progress: list[str],
    ) -> str | None:
        """Phase 2: non-streaming rewrite \u2014 conclusions only."""
        if not settings.hermes_api_key or not draft.strip():
            return None

        prompt = self._build_finalize_prompt(draft, user_request, tool_progress)
        headers = {
            "Authorization": f"Bearer {settings.hermes_api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.post(
                    f"{settings.hermes_api_url}/chat/completions",
                    headers=headers,
                    json={
                        "model": settings.hermes_model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.3,
                        "max_tokens": 2000,
                        "stream": False,
                        "stream_tool_progress": False,
                    },
                )
                if resp.status_code != 200:
                    log.warning("Phase-2 summary: Hermes API returned %d", resp.status_code)
                    return None
                data = resp.json()
                choices = data.get("choices", [])
                if not choices:
                    return None
                content = choices[0].get("message", {}).get("content", "")
                result = content.strip() if content else None
                if result:
                    log.info("Phase-2 summary succeeded: len=%d", len(result))
                return result
        except Exception:
            log.warning("Phase-2 summary failed", exc_info=True)
            return None

    async def _keep_run_alive(
        self, run_id: str, ws: str, tracker: DiscoveryTracker | None = None
    ) -> None:
        """Periodically emit ephemeral thoughts to keep the run from going stale."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            try:
                ctx = tracker.keepalive_context() if tracker else "Working on it..."
                await self.plane.create_activity(
                    ws, run_id,
                    ActivityType.thought, body=ctx, ephemeral=True,
                )
                log.debug("Keepalive thought sent for run %s", run_id[:8])
            except Exception:
                log.warning("Keepalive activity failed for run %s", run_id[:8])


# ── Rate Limiter ───────────────────────────────────────────────────


class SlidingWindowRateLimiter:
    """Sliding-window rate limiter."""

    def __init__(self, max_requests: int, window_s: float) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        now = time.time()
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_requests:
            return False
        self._timestamps.append(now)
        return True

    @property
    def current_count(self) -> int:
        now = time.time()
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        return len(self._timestamps)


# ── Webhook Handler ──────────────────────────────────────────────────────


class AgentWebhookHandler:
    """Processes incoming Plane webhooks and manages agent runs."""

    def __init__(
        self, plane: PlaneClient, processor: TaskProcessor
    ) -> None:
        self.plane = plane
        self.processor = processor
        self._active_runs: dict[str, asyncio.Task[None]] = {}
        self._dedup_cache: dict[str, float] = {}
        self._concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
        self._rate_limiter = SlidingWindowRateLimiter(
            max_requests=RATE_LIMIT_MAX_REQUESTS,
            window_s=RATE_LIMIT_WINDOW_S,
        )

    def _check_dedup(self, key: str, ttl: float = 60.0) -> bool:
        now = time.time()
        if now % 100 < 1:
            self._dedup_cache = {
                k: v for k, v in self._dedup_cache.items()
                if now - v < 120
            }
        if key in self._dedup_cache and now - self._dedup_cache[key] < ttl:
            return True
        self._dedup_cache[key] = now
        return False

    async def handle_event(
        self, payload: dict[str, Any]
    ) -> str:
        """Route a webhook event to the appropriate handler."""
        event_type = payload.get("type", "")
        action = payload.get("action", "")

        log.info("Plane webhook: type=%s action=%s", event_type, action)

        # ── Agent Run Events ──
        if event_type == "agent_run" and action in ("created",):
            return await self._handle_run_created(payload)

        if event_type == "agent_run_activity" and action in ("prompted",):
            return await self._handle_run_prompted(payload)

        log.info("Unhandled event type=%s action=%s", event_type, action)
        return f"unhandled ({event_type}/{action})"

    async def _resolve_workspace_slug(
        self, workspace_id: str, payload: dict[str, Any]
    ) -> str:
        """Resolve the workspace slug from config or Plane API.

        Tries, in order:
        1. Config override (PLANE_WORKSPACE_SLUG)
        2. OAuth app installation endpoint (if app_installation_id configured)
        3. Plane API workspace endpoint (by ID)
        4. Fallback to workspace ID itself
        """
        if settings.plane_workspace_slug:
            return settings.plane_workspace_slug
        # Try OAuth installation endpoint
        if self.plane._app_installation_id:
            slug = await self.plane.get_workspace_slug_from_installation()
            if slug:
                return slug
        # Try API
        try:
            ws_data = await self.plane.get_workspace(workspace_id)
            if ws_data:
                slug = ws_data.get("slug", ws_data.get("name", ""))
                if slug:
                    return slug
        except Exception:
            pass
        log.warning("Could not resolve workspace slug for %s \u2014 using ID", workspace_id)
        return workspace_id

    async def _handle_run_created(self, payload: dict[str, Any]) -> str:
        """Handle agent_run created webhook.

        Plane webhook payload (from docs):
        {
            "action": "created",
            "agent_run": {
                "id": "uuid", "agent_user": "uuid", "issue": "uuid",
                "project": "uuid", "workspace": "uuid",
                "status": "created", "type": "comment_thread",
            },
            "agent_user_id": "uuid", "app_client_id": "id",
            "issue_id": "uuid", "project_id": "uuid",
            "workspace_id": "uuid", "comment_id": "uuid",
            "type": "agent_run"
        }
        """
        agent_run = payload.get("agent_run", {})
        run_id = agent_run.get("id", "")
        work_item_id = payload.get("issue_id", agent_run.get("issue", ""))
        workspace_id = payload.get("workspace_id", agent_run.get("workspace", ""))
        project_id = payload.get("project_id", agent_run.get("project", ""))

        if not run_id:
            return "ignored (no run id)"

        dedup_key = f"run:{run_id}:created"
        if self._check_dedup(dedup_key):
            log.info("Dedup hit for run %s", run_id)
            return "deduped"

        # Check if already running
        existing = self._active_runs.get(run_id)
        if existing and not existing.done():
            log.info("Run %s already active", run_id)
            return "already running"

        workspace_slug = await self._resolve_workspace_slug(workspace_id, payload)

        run = AgentRun(
            run_id=run_id,
            work_item_id=work_item_id,
            work_item_identifier=work_item_id,  # Will be resolved when we fetch the issue
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
            project_id=project_id,
            action=RunAction.created,
            title=agent_run.get("name", ""),
        )

        task: asyncio.Task[None] = asyncio.create_task(self._run_session(run))
        self._active_runs[run_id] = task
        return f"processing (run={run_id[:8]}...)"

    async def _handle_run_prompted(self, payload: dict[str, Any]) -> str:
        """Handle agent_run_activity prompted webhook."""
        activity = payload.get("agent_run_activity", {})
        run_data = payload.get("agent_run", {})
        run_id = activity.get("agent_run", run_data.get("id", ""))
        workspace_id = payload.get("workspace_id", activity.get("workspace", ""))
        work_item_id = payload.get("issue_id", "")
        project_id = payload.get("project_id", "")

        if not run_id:
            return "ignored (no run id)"

        # Dedup on activity ID
        activity_id = activity.get("id", "")
        dedup_key = f"activity:{activity_id}" if activity_id else f"run:{run_id}:prompted"
        if self._check_dedup(dedup_key):
            log.info("Dedup hit for run %s", run_id)
            return "deduped"

        # Handle stop signal
        signal = activity.get("signal", "")
        if signal == "stop":
            existing = self._active_runs.get(run_id)
            if existing and not existing.done():
                existing.cancel()
                log.info("Stop signal received; cancelling run %s", run_id[:8])
                return "stopped (stop signal \u2014 task cancelled)"
            return "stopped (stop signal \u2014 no active task)"

        # Get user's prompt
        content = activity.get("content", {})
        user_body = content.get("body", "") if isinstance(content, dict) else ""

        workspace_slug = await self._resolve_workspace_slug(workspace_id, payload)

        run = AgentRun(
            run_id=run_id,
            work_item_id=work_item_id,
            work_item_identifier=work_item_id,
            workspace_id=workspace_id,
            workspace_slug=workspace_slug,
            project_id=project_id,
            action=RunAction.prompted,
            body=user_body,
        )

        task: asyncio.Task[None] = asyncio.create_task(self._run_session(run))
        self._active_runs[run_id] = task
        return f"processing (run={run_id[:8]}...)"

    async def _run_session(self, run: AgentRun) -> None:
        """Background task for an agent run."""
        async with self._concurrency_semaphore:
            try:
                work_item = None
                if run.work_item_id:
                    work_item = await self.plane.get_work_item(
                        run.workspace_slug, run.work_item_id,
                    )
                await self.processor.process(run, work_item)
            except asyncio.CancelledError:
                log.info("Run %s cancelled via stop signal", run.run_id[:8])
                raise
            except Exception as e:
                log.exception("Run %s crashed", run.run_id)
                try:
                    await self.plane.send_error(
                        run.workspace_slug, run.run_id,
                        f"Internal error: {e}",
                    )
                except Exception:
                    log.exception("Failed to send error activity")
            finally:
                self._active_runs.pop(run.run_id, None)
                log.info("Run %s complete", run.run_id[:8])


# ── FastAPI App ─────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan \u2014 set up and tear down clients."""
    log.info(
        "\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\n"
        "  Plane Agent starting...\n"
        "  Port: %s\n"
        "  API URL: %s\n"
        "\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550",
        PORT, settings.plane_api_url,
    )

    plane = PlaneClient(
        settings.plane_api_key,
        settings.plane_api_url,
        client_id=settings.plane_client_id,
        client_secret=settings.plane_client_secret,
        app_installation_id=settings.plane_app_installation_id,
    )
    processor = TaskProcessor(plane)
    handler = AgentWebhookHandler(plane, processor)

    app.state.plane = plane
    app.state.handler = handler

    log.info("Plane agent started (workspace slug: %s)",
             settings.plane_workspace_slug or "(dynamic)")

    yield

    await plane.close()
    log.info("Plane agent shut down.")


app = FastAPI(
    title="Plane Agent",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Health check endpoint."""
    return {
        "status": "ok",
        "agent": "plane-agent",
        "api_url": settings.plane_api_url,
        "installed": bool(settings.plane_api_key),
        "oauth_install_ready": settings.oauth_install_ready,
    }


@app.post("/plane/webhook")
@app.post("/webhook/plane")
async def plane_webhook(request: Request) -> Response:
    """Receive Plane webhook events (agent_run, agent_run_activity)."""
    # 1. Log delivery ID (from X-Plane-Delivery header) for traceability
    delivery_id = request.headers.get("X-Plane-Delivery", "")
    event_type_header = request.headers.get("X-Plane-Event", "")

    # 2. Read body
    body = await request.body()

    # 3. Verify HMAC signature (X-Plane-Signature header)
    signature = request.headers.get("X-Plane-Signature", "")
    if not verify_hmac(body, signature, settings.plane_webhook_secret):
        # Also try X-Hub-Signature-256 (common alternative)
        sig256 = request.headers.get("X-Hub-Signature-256", "")
        if sig256.startswith("sha256="):
            sig256 = sig256[7:]
        if not sig256 or not verify_hmac(body, sig256, settings.plane_webhook_secret):
            log.warning("HMAC verification failed")
            raise HTTPException(status_code=401, detail="Invalid signature")

    # 3. Check rate limit
    handler: AgentWebhookHandler = request.app.state.handler
    if not handler._rate_limiter.allow():
        retry_after = int(RATE_LIMIT_WINDOW_S)
        log.warning(
            "Rate limit exceeded (%d/%d) \u2014 returning 429",
            handler._rate_limiter.current_count, RATE_LIMIT_MAX_REQUESTS,
        )
        raise HTTPException(
            status_code=429,
            detail=(
                f"Rate limit exceeded. Max {RATE_LIMIT_MAX_REQUESTS} requests "
                f"per {RATE_LIMIT_WINDOW_S}s. Please retry after {retry_after}s."
            ),
            headers={"Retry-After": str(retry_after)},
        )

    # 4. Parse payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 5. Route event (background, HTTP 200 fast)
    status = await handler.handle_event(payload)
    log.info("Event handled: %s", status)

    return Response(
        content=json.dumps({"status": status}),
        media_type="application/json",
        status_code=200,
    )


# ── OAuth install flow routes ────────────────────────────────────────────


@app.get("/plane/install")
async def plane_install() -> RedirectResponse:
    """OAuth Setup URL — redirect users to Plane's consent screen."""
    if not settings.oauth_install_ready:
        raise HTTPException(
            status_code=503,
            detail=(
                "OAuth install is not configured. Set PLANE_CLIENT_ID, "
                "PLANE_CLIENT_SECRET, and PLANE_PUBLIC_URL (or PLANE_REDIRECT_URI)."
            ),
        )

    consent_url = build_plane_consent_url(
        client_id=settings.plane_client_id,
        redirect_uri=settings.effective_redirect_uri,
        scopes=settings.plane_scopes,
        api_url=settings.plane_api_url,
    )
    log.info("Redirecting to Plane OAuth consent screen")
    return RedirectResponse(url=consent_url, status_code=302)


@app.get("/plane/oauth/callback")
async def plane_oauth_callback(
    request: Request,
    app_installation_id: str | None = Query(default=None),
    code: str | None = Query(default=None),
) -> HTMLResponse:
    """OAuth Redirect URI — exchange installation ID for a bot token."""
    if not app_installation_id:
        raise HTTPException(status_code=400, detail="Missing app_installation_id")
    if not settings.plane_client_id or not settings.plane_client_secret:
        raise HTTPException(
            status_code=503,
            detail="PLANE_CLIENT_ID and PLANE_CLIENT_SECRET must be configured.",
        )

    try:
        token_data = await exchange_bot_token(
            client_id=settings.plane_client_id,
            client_secret=settings.plane_client_secret,
            app_installation_id=app_installation_id,
            scopes=settings.plane_scopes,
            api_url=settings.plane_api_url,
        )
    except RuntimeError as e:
        log.exception("OAuth callback failed during token exchange")
        raise HTTPException(status_code=502, detail=str(e)) from e

    bot_token = str(token_data["access_token"])
    installation = await fetch_app_installation(
        bot_token=bot_token,
        app_installation_id=app_installation_id,
        api_url=settings.plane_api_url,
    )

    workspace_slug = ""
    bot_user_id = ""
    if installation:
        ws_detail = installation.get("workspace_detail", {})
        workspace_slug = str(ws_detail.get("slug", "") or "")
        bot_user_id = str(installation.get("app_bot", "") or "")

    settings.plane_api_key = bot_token
    settings.plane_app_installation_id = app_installation_id
    if workspace_slug:
        settings.plane_workspace_slug = workspace_slug
    if bot_user_id:
        settings.plane_agent_user_id = bot_user_id

    plane: PlaneClient = request.app.state.plane
    plane.update_credentials(
        api_key=bot_token,
        app_installation_id=app_installation_id,
    )

    env_updates = {
        "PLANE_API_KEY": bot_token,
        "PLANE_APP_INSTALLATION_ID": app_installation_id,
    }
    if workspace_slug:
        env_updates["PLANE_WORKSPACE_SLUG"] = workspace_slug
    if bot_user_id:
        env_updates["PLANE_AGENT_USER_ID"] = bot_user_id

    env_path = Path(os.getenv("PLANE_ENV_FILE", ".env"))
    try:
        update_env_file(env_path, env_updates)
        env_saved = True
    except OSError as e:
        log.warning("Could not persist OAuth credentials to %s: %s", env_path, e)
        env_saved = False

    log.info(
        "Plane app installed (installation=%s workspace=%s code_present=%s)",
        app_installation_id[:8],
        workspace_slug or "(unknown)",
        bool(code),
    )

    env_note = (
        f"Credentials saved to <code>{env_path}</code>."
        if env_saved
        else (
            f"Could not write <code>{env_path}</code>; copy "
            "<code>PLANE_API_KEY</code> and "
            "<code>PLANE_APP_INSTALLATION_ID</code> manually."
        )
    )
    workspace_note = (
        f"<p>Workspace: <strong>{workspace_slug}</strong></p>"
        if workspace_slug
        else ""
    )

    return HTMLResponse(
        content=(
            "<!DOCTYPE html><html><head><title>Plane Agent Installed</title></head>"
            "<body style='font-family: system-ui, sans-serif; max-width: 40rem; "
            "margin: 3rem auto; line-height: 1.5;'>"
            "<h1>Plane Agent installed</h1>"
            "<p>The Hermes Plane agent is now authorized for this workspace.</p>"
            f"{workspace_note}"
            f"<p>{env_note}</p>"
            "<p>You can close this tab and @-mention the agent in a work item.</p>"
            "</body></html>"
        ),
        status_code=200,
    )


@app.get("/")
@app.get("/setup")
@app.get("/oauth/callback")
async def setup_page(request: Request) -> Response:
    """Legacy setup page — redirects to /plane/install or /plane/oauth/callback.

    Maintains backward compatibility with existing OAuth app registration
    that uses / as Setup URL and /oauth/callback as Redirect URI.
    """
    app_installation_id = request.query_params.get("app_installation_id", "")
    code = request.query_params.get("code", "")

    # If we have an app_installation_id, delegate to the proper callback handler
    if app_installation_id:
        # Rebuild the URL with the same query params
        query = urlencode({
            k: v for k, v in request.query_params.items()
        })
        redirect_url = f"/plane/oauth/callback?{query}"
        return RedirectResponse(url=redirect_url, status_code=307)

    # Otherwise, show setup info or redirect to install
    authorize_url = (
        f"{settings.plane_api_url.rstrip('/')}/auth/o/authorize-app/"
        f"?client_id={settings.plane_client_id}"
        f"&response_type=code"
        f"&redirect_uri={quote(settings.effective_redirect_uri)}"
        f"&scope={quote(settings.plane_scopes)}"
    ) if settings.oauth_install_ready else ""

    if settings.oauth_install_ready and not code:
        return RedirectResponse(url="/plane/install", status_code=302)

    html = f"""<!DOCTYPE html>
<html>
<head><title>Plane Agent — Setup</title>
<style>
body {{ font-family: sans-serif; max-width: 600px; margin: 40px auto; line-height: 1.6; }}
code {{ background: #f1f5f9; padding: 2px 6px; border-radius: 3px; font-size: 14px; }}
</style></head>
<body>
<h1>Plane Agent Setup</h1>"""
    if code:
        html += f"<p><strong>code:</strong> <code>{code}</code></p>"
    if authorize_url:
        html += f'<p><a href="{authorize_url}">Install Plane Agent</a></p>'
    else:
        html += (
            "<p>OAuth install is not configured. Set PLANE_CLIENT_ID, "
            "PLANE_CLIENT_SECRET, and PLANE_PUBLIC_URL.</p>"
        )
    html += "</body></html>"
    return Response(
        content=html, media_type="text/html", status_code=200,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point."""
    import uvicorn
    uvicorn.run(
        "plane_agent:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
