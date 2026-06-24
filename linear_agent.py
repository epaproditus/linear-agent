#!/usr/bin/env python3
"""
Linear AI Agent — Hermes-powered autonomous agent for Linear.

Integrates with Linear's Agent Session API so you can @-mention this agent
in issues, delegate tasks to it, and have it respond with typed activities
(thought → action → response), update issues, add comments, and even
delegate coding tasks to Claude Code / Codex CLI.

Architecture
────────────
Linear Webhook POST → HMAC verify → IP allowlist → Event router
  → Background asyncio Task
    → Acknowledge (thought activity within 10s)
    → Parse promptContext (issue, comments, guidance)
    → Process task (analyze, research, code)
    → Emit action activities for progress
    → Emit response activity with result
    → Update issue (comment, status, assignee)

Port: 8660
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import textwrap
import time
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("linear-agent")

# ── Constants ────────────────────────────────────────────────────────────
LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"
LINEAR_IPS = frozenset({
    "35.231.147.212",
    "35.243.126.216",
    "35.237.252.73",
    "35.243.99.142",
    "35.231.139.134",
    "35.231.103.77",
})
WEBHOOK_TIMEOUT_S = 5      # Must HTTP 200 within 5s
ACTIVITY_TIMEOUT_S = 10    # Must emit first activity within 10s
KEEPALIVE_INTERVAL_S = 15  # Emit keepalive activity every 15s during LLM processing (was 45s)
PORT = 8660

# ── Rate Limiting ──
MAX_CONCURRENT_SESSIONS = 10    # Max concurrent LLM session handlers
RATE_LIMIT_WINDOW_S = 60       # Sliding window duration (seconds)
RATE_LIMIT_MAX_REQUESTS = 30   # Max webhook requests per window

# ── Config ───────────────────────────────────────────────────────────────


class Settings(BaseSettings):
    """Environment-based configuration."""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    linear_api_key: str = ""
    """Linear API key (OAuth access token or personal API key)."""

    linear_webhook_secret: str = ""
    """HMAC signing secret from Linear webhook settings."""

    linear_agent_bot_name: str = "Hermes"
    """How the agent self-identifies; also used in self-loop prevention."""

    linear_agent_user_id: str = ""
    """If known, the agent's own user UUID for self-loop prevention."""

    linear_team_ids: str = ""
    """Comma-separated list of team IDs the agent is allowed to act on."""

    allowed_team_ids_str: str = ""
    """Alias: comma-separated team ID allowlist (also reads ALLOWED_TEAM_IDS)."""

    linear_enforce_ip_allowlist: bool = True
    """If true, only accept webhooks from known IPs."""

    allowed_ips: str = ""
    """Comma-separated custom IP allowlist. Merged with known Linear IPs."""

    hermes_api_url: str = "http://127.0.0.1:8642/v1"
    """Hermes API server URL for LLM reasoning."""

    hermes_api_key: str = ""
    """API server key for authentication."""

    hermes_model: str = "hermes-agent"
    """Model name to use via Hermes API."""

    coding_agent: str = "claude"
    """Which coding backend to delegate to: 'claude', 'codex', or 'none'."""

    coding_workdir: str = str(Path.home() / "linear-agent" / "workspace")
    """Working directory for coding agents."""

    @property
    def allowed_team_ids(self) -> set[str]:
        """Team IDs from LINEAR_TEAM_IDS and/or ALLOWED_TEAM_IDS."""
        ids: set[str] = set()
        for raw in (self.linear_team_ids, self.allowed_team_ids_str):
            for tid in raw.split(","):
                tid = tid.strip()
                if tid:
                    ids.add(tid)
        return ids

    @property
    def allowed_ips_set(self) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        """Merged set of known Linear IPs + custom ALLOWED_IPS env var."""
        ips = set(LINEAR_IPS)
        for ip_str in self.allowed_ips.split(","):
            ip_str = ip_str.strip()
            if ip_str:
                ips.add(ip_str)
        return {ipaddress.ip_address(i) for i in ips}

    @property
    def configured(self) -> bool:
        return bool(self.linear_api_key) and bool(self.linear_webhook_secret)


settings = Settings()

# Safety: don't run without config
assert settings.configured, (
    "LINEAR_API_KEY and LINEAR_WEBHOOK_SECRET must be set. "
    "Copy .env.example to .env and fill in your credentials."
)

# ── GraphQL Queries & Mutations ──────────────────────────────────────────

GQL_CREATE_ACTIVITY = """
mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) {
    success
  }
}
"""

GQL_UPDATE_SESSION = """
mutation AgentSessionUpdate($id: String!, $input: AgentSessionUpdateInput!) {
  agentSessionUpdate(id: $id, input: $input) {
    success
  }
}
"""

GQL_ISSUE_BY_ID = """
query Issue($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    priority
    url
    state { id name type }
    team { id key name }
    project { id name }
    assignee { id name email }
    delegate { id name email }
    labels { nodes { id name } }
    comments {
      nodes {
        id
        body
        createdAt
        user { id name }
        children {
          nodes {
            id
            body
            createdAt
            user { id name }
          }
        }
      }
    }
  }
}
"""

GQL_ISSUE_BY_IDENTIFIER = """
query IssueByIdentifier($id: String!) {
  issue(id: $id) {
    id
    identifier
    title
    description
    priority
    url
    state { id name type }
    team { id key name }
    assignee { id name email }
    labels { nodes { id name } }
  }
}
"""

GQL_ISSUE_CREATE = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue {
      id
      identifier
      url
      title
    }
  }
}
"""

GQL_CREATE_COMMENT = """
mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment { id body }
  }
}
"""

GQL_UPDATE_ISSUE = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier state { id name } }
  }
}
"""

GQL_TEAM_STATES = """
query TeamStates($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name type }
  }
}
"""

GQL_TEAMS = """
query Teams {
  teams {
    nodes { id name key description }
  }
}
"""

GQL_VIEWER = """
query Me {
  viewer { id name email }
}
"""

GQL_PROJECTS = """
query Projects {
  projects {
    nodes {
      id
      name
      description
      url
      teams { nodes { id name key } }
    }
  }
}
"""

GQL_PROJECT_CREATE = """
mutation ProjectCreate($input: ProjectCreateInput!) {
  projectCreate(input: $input) {
    success
    project { id name url }
  }
}
"""

GQL_CREATE_SESSION_ON_ISSUE = """
mutation AgentSessionCreateOnIssue($input: AgentSessionCreateOnIssueInput!) {
  agentSessionCreateOnIssue(input: $input) {
    success
    agentSession { id }
  }
}
"""

GQL_SESSION_ACTIVITIES = """
query AgentSessionActivities($id: String!) {
  agentSession(id: $id) {
    activities {
      edges {
        node {
          updatedAt
          content {
            __typename
            ... on AgentActivityThoughtContent { body }
            ... on AgentActivityActionContent { action parameter result }
            ... on AgentActivityElicitationContent { body }
            ... on AgentActivityResponseContent { body }
            ... on AgentActivityErrorContent { body }
            ... on AgentActivityPromptContent { body }
          }
        }
      }
    }
  }
}
"""

# ── Data Models ──────────────────────────────────────────────────────────


class ActivityType(str, Enum):
    thought = "thought"
    elicitation = "elicitation"
    action = "action"
    response = "response"
    error = "error"


class SessionAction(str, Enum):
    created = "created"
    prompted = "prompted"


@dataclass
class AgentSession:
    """Represents an active agent session from a webhook event."""

    session_id: str
    issue_id: str
    issue_identifier: str
    action: SessionAction
    prompt_context: str  # Raw XML promptContext string
    body: str = ""         # Current user message (new prompt for 'prompted', comment for 'created')
    original_body: str = ""  # For 'prompted': the session-creating @mention text
    # Individual fields
    title: str = ""
    description: str = ""
    team_id: str = ""
    team_key: str = ""
    team_name: str = ""
    priority: int = 0
    labels: list[str] = field(default_factory=list)
    assignee_name: str = ""
    assignee_email: str = ""
    state_name: str = ""
    state_type: str = ""
    comments: list[dict] = field(default_factory=list)
    guidance: list[str] = field(default_factory=list)
    previous_activities: list[dict] = field(default_factory=list)


# ── Linear GraphQL Client ───────────────────────────────────────────────


class LinearClient:
    """Async HTTPX client for the Linear GraphQL API."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._client: httpx.AsyncClient | None = None
        self._viewer_id: str | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=LINEAR_GRAPHQL_URL,
                headers={
                    "Authorization": self._api_key,
                    "Content-Type": "application/json",
                    "User-Agent": "HermesLinearAgent/1.0",
                },
                timeout=httpx.Timeout(30.0),
            )
        return self._client

    async def _gql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a GraphQL query/mutation and return the data."""
        client = await self._get_client()
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = await client.post("", json=payload)
        body = resp.json()

        if "errors" in body:
            raise RuntimeError(
                f"GraphQL error: {body['errors']} "
                f"(query: {query[:120]}...)"
            )
        return body.get("data", {})

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Viewer / Identity ──

    async def get_viewer_id(self) -> str:
        """Get the agent's own user ID from Linear."""
        if self._viewer_id:
            return self._viewer_id
        data = await self._gql(GQL_VIEWER)
        self._viewer_id = data["viewer"]["id"]
        return self._viewer_id

    async def get_viewer(self) -> dict[str, Any]:
        """Get full viewer info."""
        data = await self._gql(GQL_VIEWER)
        return data["viewer"]

    # ── Issues ──

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        """Fetch a single issue by UUID or identifier."""
        try:
            data = await self._gql(GQL_ISSUE_BY_ID, {"id": issue_id})
            return data.get("issue")
        except RuntimeError:
            return None

    async def get_issue_by_identifier(self, identifier: str) -> dict[str, Any] | None:
        """Fetch an issue by its identifier (e.g. 'ENG-123')."""
        try:
            data = await self._gql(GQL_ISSUE_BY_IDENTIFIER, {"id": identifier})
            return data.get("issue")
        except RuntimeError:
            return None

    async def comment(self, issue_id: str, body: str, parent_id: str | None = None) -> bool:
        """Add a comment to an issue, optionally as a threaded reply."""
        variables: dict[str, Any] = {"input": {"issueId": issue_id, "body": body}}
        if parent_id:
            variables["input"]["parentId"] = parent_id  # type: ignore[typeddict-item]
        data = await self._gql(GQL_CREATE_COMMENT, variables)
        return data.get("commentCreate", {}).get("success", False)

    async def update_issue(
        self, issue_id: str, **kwargs: Any
    ) -> bool:
        """Update issue fields (stateId, assigneeId, priority, etc.)."""
        data = await self._gql(GQL_UPDATE_ISSUE, {
            "id": issue_id,
            "input": kwargs,
        })
        return data.get("issueUpdate", {}).get("success", False)

    async def get_team_states(self, team_id: str) -> list[dict[str, Any]]:
        """Get workflow states for a team."""
        data = await self._gql(GQL_TEAM_STATES, {"teamId": team_id})
        return data.get("workflowStates", {}).get("nodes", [])

    async def list_teams(self) -> list[dict[str, Any]]:
        """List all teams visible to the agent."""
        data = await self._gql(GQL_TEAMS)
        return data.get("teams", {}).get("nodes", [])

    async def list_projects(self) -> list[dict[str, Any]]:
        """List all projects visible to the agent."""
        data = await self._gql(GQL_PROJECTS)
        return data.get("projects", {}).get("nodes", [])

    async def find_project_by_name(
        self, name: str, exact: bool = False
    ) -> dict[str, Any] | None:
        """Find a project by name (case-insensitive). Returns the first match or None.

        Args:
            name: Project name to search for.
            exact: If True, requires exact match (case-insensitive).
                   If False (default), matches if the search term is contained in the name.
        """
        projects = await self.list_projects()
        name_lower = name.lower()
        for p in projects:
            p_name = p.get("name", "").lower()
            if exact and p_name == name_lower:
                return p
            if not exact and name_lower in p_name:
                return p
        return None

    async def create_project(
        self,
        name: str,
        team_ids: list[str],
        description: str = "",
    ) -> dict[str, Any] | None:
        """Create a new Linear project. Returns the project dict or None on failure."""
        inp: dict[str, Any] = {"name": name, "teamIds": team_ids}
        if description:
            inp["description"] = description
        data = await self._gql(GQL_PROJECT_CREATE, {"input": inp})
        result = data.get("projectCreate", {})
        if result.get("success"):
            return result.get("project")
        return None

    async def find_state_by_type(
        self, team_id: str, state_type: str, preferred_name: str | None = None
    ) -> str | None:
        """Find a workflow state UUID by type. If preferred_name given, tries name match first."""
        states = await self.get_team_states(team_id)
        if preferred_name:
            for s in states:
                if s["type"] == state_type and s["name"].lower() == preferred_name.lower():
                    return s["id"]
        for s in states:
            if s["type"] == state_type:
                return s["id"]
        return None

    # ── Agent Activities ──

    async def create_activity(
        self,
        session_id: str,
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
        """Emit an agent activity to the session.

        Args:
            session_id: The AgentSession ID.
            activity_type: Type of activity (thought, action, response, error).
            body: Main text content (Markdown OK).
            action_label: For 'action' type: the action name (e.g. 'Searching').
            action_param: For 'action' type: the parameter (e.g. query string).
            action_result: For 'action' type: the result (Markdown OK).
            ephemeral: If True, replaced by next activity.
            signal: Optional signal ('auth', 'select') for elicitation activities.
            signal_metadata: Metadata dict for the signal (auth URL, select options).
        """
        content: dict[str, Any] = {"type": activity_type.value}

        if activity_type == ActivityType.action:
            content["action"] = action_label if action_label else ""
            content["parameter"] = action_param or ""
            content["body"] = body
            if action_result:
                content["result"] = action_result
        else:
            content["body"] = body

        inp: dict[str, Any] = {
            "agentSessionId": session_id,
            "content": json.dumps(content),
            "ephemeral": ephemeral,
        }
        if signal:
            inp["signal"] = signal
        if signal_metadata:
            inp["signalMetadata"] = json.dumps(signal_metadata)

        data = await self._gql(GQL_CREATE_ACTIVITY, {"input": inp})
        return data.get("agentActivityCreate", {}).get("success", False)

    async def acknowledge(self, session_id: str, message: str = "") -> bool:
        """Send 'thought' activity — required within 10s of a 'created' event."""
        return await self.create_activity(
            session_id,
            ActivityType.thought,
            body=message or "Hermes agent here. Processing the issue...",
            ephemeral=True,
        )

    async def send_action(
        self,
        session_id: str,
        label: str,
        param: str,
        body: str = "",
        result: str | None = None,
        ephemeral: bool = True,
    ) -> bool:
        """Emit an 'action' activity (visible progress step, ephemeral by default)."""
        return await self.create_activity(
            session_id,
            ActivityType.action,
            body=body or f"**{label}**...",
            action_label=label,
            action_param=param,
            action_result=result,
            ephemeral=ephemeral,
        )

    async def send_response(self, session_id: str, body: str) -> bool:
        """Emit the final 'response' activity."""
        result = await self.create_activity(
            session_id, ActivityType.response, body=body,
        )
        log.info(
            "send_response(%s): success=%s response_len=%d",
            session_id[:8], result, len(body),
        )
        return result

    async def send_error(self, session_id: str, body: str) -> bool:
        """Emit an 'error' activity."""
        result = await self.create_activity(
            session_id, ActivityType.error, body=body,
        )
        log.warning(
            "send_error(%s): success=%s error_len=%d",
            session_id[:8], result, len(body),
        )
        return result

    async def send_elicitation(self, session_id: str, body: str) -> bool:
        """Ask a clarification question."""
        return await self.create_activity(
            session_id, ActivityType.elicitation, body=body,
        )

    async def send_elicitation_select(
        self,
        session_id: str,
        body: str,
        options: list[dict[str, str]],
    ) -> bool:
        """Present a list of options for the user to choose from."""
        return await self.create_activity(
            session_id,
            ActivityType.elicitation,
            body=body,
            signal="select",
            signal_metadata={"options": options},
        )

    async def send_elicitation_auth(
        self,
        session_id: str,
        body: str,
        url: str,
        *,
        user_id: str | None = None,
        provider_name: str | None = None,
    ) -> bool:
        """Request account linking before the agent can continue."""
        meta: dict[str, Any] = {"url": url}
        if user_id:
            meta["userId"] = user_id
        if provider_name:
            meta["providerName"] = provider_name
        return await self.create_activity(
            session_id,
            ActivityType.elicitation,
            body=body,
            signal="auth",
            signal_metadata=meta,
        )

    async def update_plan(
        self,
        session_id: str,
        steps: list[dict[str, str]],
    ) -> bool:
        """Replace the session's plan checklist (full array required each time)."""
        data = await self._gql(GQL_UPDATE_SESSION, {
            "id": session_id,
            "input": {"plan": steps},
        })
        return data.get("agentSessionUpdate", {}).get("success", False)

    async def get_session_activities(
        self, session_id: str
    ) -> list[dict[str, Any]]:
        """Fetch all activities for a session (for conversation reconstruction)."""
        try:
            data = await self._gql(GQL_SESSION_ACTIVITIES, {"id": session_id})
            edges = (
                data.get("agentSession", {})
                    .get("activities", {})
                    .get("edges", [])
            )
            return [e["node"] for e in edges]
        except RuntimeError:
            return []

    async def update_session(
        self,
        session_id: str,
        *,
        external_urls: list[dict[str, str]] | None = None,
        summary: str | None = None,
    ) -> bool:
        """Update session metadata."""
        inp: dict[str, Any] = {}
        if external_urls is not None:
            inp["externalUrls"] = [{"label": u["label"], "url": u["url"]}
                                   for u in external_urls]
        if summary is not None:
            inp["summary"] = summary
        data = await self._gql(GQL_UPDATE_SESSION, {
            "id": session_id,
            "input": inp,
        })
        return data.get("agentSessionUpdate", {}).get("success", False)


# ── Coding Agent Bridge ─────────────────────────────────────────────────


class CodingBridge:
    """Delegates coding tasks to Claude Code or Codex CLI."""

    def __init__(self, backend: str, workdir: str) -> None:
        self.backend = backend
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)

    async def run(
        self,
        issue_id: str,
        title: str,
        description: str,
        context: str = "",
    ) -> dict[str, Any]:
        """Execute a coding task for the given issue.

        Returns a dict with 'success', 'output', and 'error' keys.
        """
        # Clone or create working directory
        repo_dir = self.workdir / issue_id.replace("-", "_")
        repo_dir.mkdir(parents=True, exist_ok=True)

        task_prompt = textwrap.dedent(f"""\
        Issue: {title}

        {description or "(no description)"}

        {context}

        ---
        Please implement the solution. Run any relevant tests.
        Do NOT create a git commit — the agent will handle that.
        """).strip()

        if self.backend == "claude":
            return await self._run_claude(repo_dir, task_prompt)
        elif self.backend == "codex":
            return await self._run_codex(repo_dir, task_prompt)
        else:
            return {
                "success": False,
                "output": "",
                "error": f"Unknown coding backend: {self.backend}",
            }

    async def run_parallel(
        self,
        issue_id: str,
        title: str,
        description: str,
        context: str = "",
    ) -> list[dict[str, Any]]:
        """Execute coding tasks on all available backends in parallel.

        When backend is 'all', runs both Claude Code and Codex CLI simultaneously.
        For single-backend config, delegates to run() and returns a single-result list.
        """
        if self.backend == "all":
            backends = ["claude", "codex"]
        else:
            return [await self.run(issue_id, title, description, context)]

        async def _run_backend(b: str) -> dict[str, Any]:
            bridge = CodingBridge(b, str(self.workdir))
            return await bridge.run(issue_id, title, description, context)

        log.info("Running subagents in parallel: %s", ", ".join(backends))
        results: list[dict[str, Any]] = []
        for result in await asyncio.gather(
            *[_run_backend(b) for b in backends], return_exceptions=True
        ):
            if isinstance(result, BaseException):
                results.append({
                    "success": False,
                    "output": "",
                    "error": f"Parallel subagent error: {result}",
                })
            else:
                results.append(result)
        return results

    async def _run_claude(
        self, workdir: Path, prompt: str
    ) -> dict[str, Any]:
        """Delegate to Claude Code CLI."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "--print",
                prompt,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "CLAUDE_CODE_VERBOSE": "0"},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300.0
            )
            return {
                "success": proc.returncode == 0,
                "output": stdout.decode(errors="replace"),
                "error": stderr.decode(errors="replace")
                if proc.returncode != 0
                else "",
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {
                "success": False,
                "output": "",
                "error": "Claude Code timed out after 300s",
            }
        except FileNotFoundError:
            return {
                "success": False,
                "output": "",
                "error": "Claude Code CLI not found. Install with: "
                "npm install -g @anthropic/claude-code",
            }

    async def _run_codex(
        self, workdir: Path, prompt: str
    ) -> dict[str, Any]:
        """Delegate to OpenAI Codex CLI."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "codex",
                "exec",
                prompt,
                cwd=str(workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300.0
            )
            return {
                "success": proc.returncode == 0,
                "output": stdout.decode(errors="replace"),
                "error": stderr.decode(errors="replace")
                if proc.returncode != 0
                else "",
            }
        except asyncio.TimeoutError:
            proc.kill()
            return {
                "success": False,
                "output": "",
                "error": "Codex CLI timed out after 300s",
            }
        except FileNotFoundError:
            return {
                "success": False,
                "output": "",
                "error": "Codex CLI not found. Install with: "
                "npm install -g @openai/codex",
            }


# ── Webhook Security ────────────────────────────────────────────────────


def verify_hmac(payload: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 verification with timing-safe comparison."""
    expected = hmac.new(
        secret.encode(), payload, "sha256"
    ).hexdigest()
    # Linear sends the raw hex hash as the header value (no "sha256=" prefix)
    # But also accept the prefixed form for compatibility
    if signature.startswith("sha256="):
        signature = signature[7:]
    result = hmac.compare_digest(expected, signature)
    if not result:
        log.warning(
            "HMAC mismatch: computed_hash=%s... received_hash=%s... payload_len=%d secret_len=%d",
            expected[:16], str(signature)[:16], len(payload), len(secret),
        )
    return result


def verify_ip(request: Request) -> bool:
    """Check if the request comes from a known Linear IP or custom ALLOWED_IPS."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip_str = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else ""
    )
    try:
        return ipaddress.ip_address(ip_str) in settings.allowed_ips_set
    except ValueError:
        return False


# ── Prompt Context Parser ───────────────────────────────────────────────


def _is_linear_system_comment(body: str) -> bool:
    """Detect Linear's auto-generated agent-session comment (not real user input)."""
    if not body:
        return False
    normalized = body.strip().lower()
    return normalized.startswith("this thread is for an agent session")


def parse_prompt_context(xml_str: str) -> dict[str, Any]:
    """Roughly parse the Linear promptContext XML into a dict.

    Uses simple regex — not a full XML parser, but sufficient for Linear's
    well-structured promptContext format.
    """
    result: dict[str, Any] = {}
    result["labels"] = []

    m = re.search(r'<issue identifier="([^"]+)"', xml_str)
    if m:
        result["identifier"] = m.group(1)

    m = re.search(r"<title>([^<]*)</title>", xml_str)
    if m:
        result["title"] = m.group(1)

    m = re.search(r"<description>([^<]*)</description>", xml_str)
    if m:
        result["description"] = m.group(1)

    m = re.search(r'<team name="([^"]*)"', xml_str)
    if m:
        result["team_name"] = m.group(1)

    result["labels"] = re.findall(r"<label>([^<]+)</label>", xml_str)

    # Extract guidance rules
    result["guidance"] = re.findall(
        r"<guidance-rule[^>]*>([^<]+)</guidance-rule>", xml_str
    )

    # Extract primary directive
    m = re.search(
        r"<primary-directive-thread[^>]*>.*?<comment[^>]*>.*?([^<]+)</comment>",
        xml_str,
        re.DOTALL,
    )
    if m:
        result["primary_directive"] = m.group(1).strip()

    # Count comments
    result["comment_count"] = len(re.findall(r"<comment[^>]*>", xml_str))

    return result


def _format_activities_conversation(activities: list[dict[str, Any]]) -> str:
    """Reconstruct a structured conversation from session activities.

    Sorts by updatedAt and formats prompt/response/action/thought/error activities
    into a readable conversation string for LLM context.
    Returns empty string if no meaningful conversation can be reconstructed.
    """
    sorted_acts = sorted(activities, key=lambda a: a.get("updatedAt", ""))
    lines: list[str] = []
    for act in sorted_acts:
        content = act.get("content", {})
        typename = content.get("__typename", "")
        ts = act.get("updatedAt", "")
        time_str = ts[11:19] if len(ts) > 19 else ""

        if typename == "AgentActivityPromptContent":
            body = (content.get("body") or "").strip()
            if body:
                lines.append(f"[{time_str}] **User:** {body}")

        elif typename == "AgentActivityResponseContent":
            body = (content.get("body") or "").strip()
            if body:
                lines.append(f"[{time_str}] **Hermes:** {body}")

        elif typename == "AgentActivityErrorContent":
            body = (content.get("body") or "").strip()
            if body:
                lines.append(f"[{time_str}] **Hermes (error):** {body[:200]}")

        elif typename == "AgentActivityActionContent":
            action = content.get("action", "")
            param = content.get("parameter", "")
            if action:
                label = f"**Hermes action: {action}**"
                if param:
                    label += f" — {param}"
                lines.append(f"[{time_str}] {label}")

        elif typename == "AgentActivityThoughtContent":
            body = (content.get("body") or "").strip()
            if body and len(body) < 200:
                lines.append(f"[{time_str}] *Hermes thought:* {body}")

    return "\n".join(lines)


def _flatten_comments(nodes: list[dict[str, Any]], indent: int = 0) -> list[tuple[int, dict[str, Any]]]:
    """Recursively flatten threaded comments into a depth-annotated list.

    Each item is (depth, comment_dict). Top-level comments are depth 0,
    children are depth 1, grandchildren depth 2, etc.
    """
    result: list[tuple[int, dict[str, Any]]] = []
    for c in nodes:
        if c is None:
            continue
        result.append((indent, c))
        child_nodes = c.get("children", {}).get("nodes", []) or []
        if child_nodes:
            result.extend(_flatten_comments(child_nodes, indent + 1))
    return result


def _format_comment_line(depth: int, c: dict[str, Any]) -> str:
    """Format a single comment (with depth) into a readable line."""
    author = (c.get("user") or {}).get("name", "Unknown")
    body = c.get("body", "") or ""
    ts = c.get("createdAt", "")[11:19] if c.get("createdAt") else ""
    prefix = "  > " * depth if depth > 0 else "- "
    return f"[{ts}] {prefix}{author}: {body}"


# ── Discovery Tracker ──────────────────────────────────────────


@dataclass
class DiscoveryTracker:
    """Tracks and emits discovery activities during issue work.

    Unlike a phase tracker, this has no concept of phase. It provides
    helpers for emitting findings (found, identified, decided, created,
    verified) and rate-limits emission to avoid flooding Linear's API.

    Key invariant: every non-keepalive activity carries information
    the user didn't have before.

    Discovery kinds:
        Found: A piece of information located during investigation.
        Identified: A gap, pattern, or relationship recognized.
        Decided: A choice made between alternatives.
        Created: An artifact produced.
        Verified: A validation completed.

    Fire-and-forget: failures during emission are logged but never
    block or delay the caller.
    """

    session_id: str
    linear: LinearClient
    last_emit: float = 0.0
    activity_count: int = 0
    _keepalive_ctx: str = ""
    _pending_tasks: list[asyncio.Task] = field(default_factory=list)
    _skip_texts: set = field(default_factory=set)
    """Texts already emitted by the tracker; LLM-streamed duplicates are suppressed."""
    tool_progress_count: int = 0
    """Hermes tool-progress activities emitted this session (excludes keepalive)."""

    MIN_ACTIVITY_INTERVAL: float = 0.8
    """Minimum seconds between any activity emission."""
    MILESTONE_INTERVAL: float = 1.5
    """Minimum seconds between persistent (non-ephemeral) milestone activities."""

    async def found(self, detail: str) -> bool:
        """Emit a discovery: something located during investigation."""
        return await self.progress(detail)

    async def identified(self, detail: str) -> bool:
        """Emit a discovery: a gap, pattern, or relationship recognized."""
        return await self.progress(detail)

    async def decided(self, detail: str) -> bool:
        """Emit a discovery: a choice made between alternatives."""
        return await self.progress(detail)

    async def created(self, detail: str) -> bool:
        """Emit a discovery: an artifact produced."""
        return await self.progress(detail)

    async def verified(self, detail: str) -> bool:
        """Emit a discovery: a validation completed."""
        return await self.progress(detail)

    async def in_progress(self, description: str) -> bool:
        """Emit an ephemeral in-progress indicator.

        Updates keepalive context. Replaced by the next ephemeral activity.
        """
        self._keepalive_ctx = description
        self._skip_texts.add(description)
        return await self._emit("", description, ephemeral=True)

    async def progress(self, detail: str) -> bool:
        """Emit a persistent progress update without a kind label.

        Use this for tool-completion summaries and other intermediate
        results that don't warrant a 'found'/'identified' category.
        Produces natural text like cursor's output.
        """
        return await self._emit("", detail, ephemeral=False)

    async def _emit(self, kind: str, detail: str, ephemeral: bool) -> bool:
        """Rate-limited activity emission. Skips if interval hasn't elapsed.

        Uses ``thought`` type activities (not action) because ``action``
        activities have no ``body`` field in the GraphQL schema — Linear
        drops it silently.  ``thought`` activities have a ``body`` field
        that renders with proper word-wrapping in the UI.

        Fire-and-forget: the HTTP POST runs in a background task so the
        SSE streaming loop never stalls on the 200-500ms round trip.
        Pending tasks are tracked and flushed before the session is
        finalized (see ``flush()``).

        Rate-limited.  Returns True if emitted, False if rate-limited.
        """
        now = time.monotonic()
        interval = 0.5 if ephemeral else self.MILESTONE_INTERVAL
        if now - self.last_emit < interval:
            log.debug(
                "DiscoveryTracker: rate-limited %s: %s",
                kind or "progress", detail[:60],
            )
            return False
        self.last_emit = now
        self.activity_count += 1

        task = asyncio.create_task(self._do_emit(detail[:500], ephemeral))
        self._pending_tasks.append(task)
        # GC completed tasks to prevent unbounded growth
        self._pending_tasks = [t for t in self._pending_tasks if not t.done()]
        return True

    async def _do_emit(self, detail: str, ephemeral: bool) -> None:
        """Actually POST the activity to Linear. Runs in background task."""
        try:
            await self.linear.create_activity(
                self.session_id,
                ActivityType.thought,
                body=detail,
                ephemeral=ephemeral,
            )
        except Exception:
            log.warning(
                "DiscoveryTracker: failed to emit: %s",
                detail[:60],
            )

    async def flush(self) -> None:
        """Wait for all pending emit tasks to complete.

        Call before finalizing the session (before ``send_response()``)
        so all intermediate thought activities are visible in the timeline.
        """
        tasks = self._pending_tasks[:]
        self._pending_tasks.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def keepalive_context(self) -> str:
        """Return the current investigation context for keepalive messages."""
        if self._keepalive_ctx:
            # Capitalize first letter for natural reading
            return self._keepalive_ctx[0].upper() + self._keepalive_ctx[1:]
        return "Working on it..."


class ProgressQueueWorker:
    """Dedicated background worker that drains a queue of progress text
    and POSTs each item to Linear via ``tracker.progress()``.

    This decouples the SSE reading loop (which must never block on I/O)
    from the Linear GraphQL mutation (which takes 200-500ms per call).
    """

    def __init__(self, tracker: DiscoveryTracker | None) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._tracker = tracker
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Launch the background worker. Call before the SSE loop."""
        self._task = asyncio.create_task(self._run())

    async def put(self, text: str) -> None:
        """Push text to the queue (never blocks, returns instantly)."""
        await self._queue.put(text)

    async def drain_and_stop(self) -> None:
        """Wait for all queued items to be processed, then stop the worker."""
        await self._queue.join()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        """Internal: consume the queue and POST to Linear directly.

        Calls ``_do_emit`` directly (not ``tracker.progress()``) to
        avoid the redundant ``create_task`` wrapper in ``_emit()``.
        Since this worker already runs as a background task, it can
        simply await the HTTP POST — no additional indirection needed.
        Rate-limiting keeps Linear API load manageable.
        """
        last_emit = 0.0
        try:
            while True:
                text = await self._queue.get()
                if self._tracker:
                    # Rate-limit: at most one POST per interval
                    now = time.monotonic()
                    if now - last_emit >= self._tracker.MILESTONE_INTERVAL:
                        last_emit = now
                        try:
                            await self._tracker._do_emit(text[:500], ephemeral=False)
                        except Exception:
                            log.warning(
                                "ProgressQueueWorker: _do_emit failed",
                                exc_info=True,
                            )
                self._queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.error("ProgressQueueWorker: crashed", exc_info=True)
            # Prevent deadlock: mark all remaining items done so
            # drain_and_stop() doesn't hang forever.
            while not self._queue.empty():
                self._queue.get_nowait()
                self._queue.task_done()


# ── Hermes Tool Progress → Discovery Text ─────────────────────────

_HERMES_INTERNAL_TOOL_PREFIX = "_"

_HERMES_TOOL_VERBS: dict[str, str] = {
    "read_file": "Reading",
    "write_file": "Writing",
    "search_files": "Searching",
    "web_search": "Searching web",
    "web_extract": "Fetching",
    "terminal": "Running",
    "execute_code": "Running code",
    "browser": "Browsing",
    "grep": "Searching",
}

_CODE_MARKERS = (
    "import ", "def ", "class ", "subprocess", "print(", "async def",
    "asyncio.", "json.", "capture_output",
)

# Labels too vague or meta to show on the Linear timeline.
_NOISE_LABELS = frozenset({
    "fetching", "searching web", "searching", "browsing", "running",
    "hermes tool progress", "working on it", "*.log", "*.log'",
})


def _normalize_progress_markdown(text: str) -> str:
    """Strip markdown Hermes or upstream clients may have injected."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


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


def _summarize_url(url: str) -> str:
    lower = url.lower()
    if "hermes-agent.nousresearch.com" in lower:
        if "api-server" in lower:
            return "Read Hermes API server docs"
        return "Read Hermes documentation"
    if "github.com" in lower:
        issue = re.search(r"/issues/(\d+)", url)
        if issue:
            return f"Checked GitHub issue #{issue.group(1)}"
        pr = re.search(r"/pull/(\d+)", url)
        if pr:
            return f"Reviewed GitHub PR #{pr.group(1)}"
        return "Opened GitHub link"
    return "Fetched web page"


def _summarize_pipe_pattern(text: str) -> str:
    """Turn grep-style alternation patterns into one readable line."""
    parts = [
        p.strip().strip("\\")
        for p in text.split("|")
        if p.strip() and p.strip().lower() not in _NOISE_LABELS
    ]
    if not parts:
        return "Searched the codebase"
    if len(parts) == 1:
        return f"Searched for `{parts[0][:45]}`"
    shown = ", ".join(f"`{p[:28]}`" for p in parts[:3])
    if len(parts) > 3:
        return f"Searched for {shown} and {len(parts) - 3} more"
    return f"Searched for {shown}"


def _looks_like_code(text: str) -> bool:
    if len(text) > 120 or "\n" in text:
        return True
    lower = text.lower()
    return any(m in lower for m in _CODE_MARKERS)


def _looks_like_path(text: str) -> bool:
    t = text.strip()
    return (
        t.startswith("/")
        or t.startswith("~/")
        or ("/" in t and re.search(r"\.[a-zA-Z0-9]{1,6}$", t))
    )


def _is_repo_slug(text: str) -> bool:
    t = text.strip()
    return bool(re.fullmatch(r"[\w][\w.-]{0,48}", t)) and " " not in t


def _is_bare_symbol(text: str) -> bool:
    t = text.strip()
    return bool(re.fullmatch(r"_?[\w]+", t)) and len(t) >= 4


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("/"))


def _summarize_code_label(label: str) -> str | None:
    """Turn a Python/script blob into a short human-readable action."""
    for line in label.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            intent = stripped.lstrip("#").strip()
            if len(intent) >= 6:
                return intent[0].upper() + intent[1:]

    lower = label.lower()
    if "git" in lower and "log" in lower:
        m = re.search(r"-(\d+)", label)
        n = m.group(1) if m else "20"
        return f"Checked recent git history (last {n} commits)"
    if "git" in lower and "show" in lower:
        return "Reviewed latest commit"
    if "git" in lower and "diff" in lower:
        return "Compared code changes"
    if "grep" in lower or "rg " in lower or "|" in label:
        return _summarize_pipe_pattern(label)
    if "read" in lower and ("_call_llm" in label or "progress" in lower):
        return "Read progress parsing code in linear_agent.py"
    if "subprocess" in lower:
        return "Ran shell command"
    return "Ran script"


def _extract_finding_from_output(
    tool: str, output: str, label: str, args: dict[str, Any],
) -> str | None:
    """Mine a completed tool's output for a user-visible finding."""
    output = output.strip()
    if not output or len(output) < 2:
        return None

    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    non_empty = lines[:20]

    if tool in ("grep", "search_files") or "grep" in label.lower():
        file_hits: dict[str, int] = {}
        symbols: list[str] = []
        for ln in non_empty:
            m = re.match(r"^([^:]+):(\d+):?(.*)$", ln)
            if m:
                fname = _basename(m.group(1))
                file_hits[fname] = file_hits.get(fname, 0) + 1
                sym = (m.group(3) or "").strip()
                if sym and len(sym) < 60:
                    symbols.append(sym)
            elif ln and not ln.startswith("Binary"):
                symbols.append(ln[:60])
        if file_hits:
            top = max(file_hits, key=file_hits.get)
            total = sum(file_hits.values())
            if symbols and len(symbols) == 1:
                sym = symbols[0].removeprefix("def ").strip()
                return f"Found `{sym}` in `{top}`"
            return f"Found {total} matches in `{top}`"
        if non_empty:
            return f"Found {len(non_empty)} search results"

    if tool == "read_file" or (tool == "terminal" and _looks_like_path(label)):
        path = str(args.get("path") or label or "file")
        base = _basename(path) if path else "file"
        line_count = output.count("\n") + 1
        return f"Read `{base}` ({line_count} lines)"

    if tool == "terminal":
        lower_label = label.lower()
        if "git log" in lower_label or any(
            re.match(r"^[0-9a-f]{7,}\s", ln) for ln in non_empty[:3]
        ):
            count = sum(
                1 for ln in non_empty if re.match(r"^[0-9a-f]{7,}\s", ln)
            )
            return f"Found {count or len(non_empty)} recent commits"
        if "git show" in lower_label or "git diff" in lower_label:
            return "Reviewed latest commit changes"
        if len(non_empty) == 1 and len(non_empty[0]) <= 80:
            return f"Got `{non_empty[0]}`"
        if non_empty:
            preview = non_empty[0][:60]
            return f"Command returned: `{preview}`"

    if tool == "web_search":
        if non_empty:
            return f"Found {len(non_empty)} web results"
        return None

    if tool == "web_extract":
        if len(output) > 100:
            return f"Fetched page ({len(output)} chars)"
        return None

    if tool == "execute_code" and non_empty:
        preview = non_empty[0][:60]
        return f"Script output: `{preview}`"

    return None


def _beautify_progress_text(
    tool: str, label: str, args: dict[str, Any],
) -> str | None:
    """Convert raw Hermes labels into short, consistent timeline prose."""
    label = _normalize_progress_markdown(label)
    if label.lower().startswith("found "):
        label = label[6:].strip()

    if not label:
        return _format_hermes_tool_fallback(tool, args)

    if _is_noise_label(label):
        return None

    if _is_url(label):
        return _summarize_url(label)

    if "|" in label and len(label) < 140:
        return _summarize_pipe_pattern(label)

    if _is_bare_symbol(label):
        return f"Found `{label}`"

    if _is_repo_slug(label) and tool not in ("terminal", "execute_code", "browser"):
        return None

    if _is_repo_slug(label):
        return f"Working in `{label}`"

    if _looks_like_path(label):
        base = _basename(label)
        if base.startswith(".") or base.endswith(
            (".py", ".md", ".json", ".yaml", ".yml", ".toml", ".env", ".example")
        ):
            return f"Read `{base}`"
        return f"Opened `{base}`"

    sym = re.fullmatch(r"def (\w+)", label.strip())
    if sym:
        return f"Found `{sym.group(1)}`"

    if tool in ("execute_code", "terminal") and _looks_like_code(label):
        return _summarize_code_label(label)

    if tool == "terminal" and len(label) <= 72 and not _looks_like_code(label):
        return f"Ran `{label}`"

    if tool == "web_search" and len(label) <= 80 and not _is_url(label):
        return f"Searched web for `{label}`"

    if tool == "web_extract" and _is_url(label):
        return _summarize_url(label)

    if len(label) <= 80 and not _looks_like_code(label):
        return label

    return _format_hermes_tool_fallback(tool, args) or None


def _parse_hermes_tool_args(event: dict) -> dict[str, Any]:
    """Normalize tool args from a hermes.tool.progress payload."""
    raw = event.get("input") or event.get("args") or event.get("arguments")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": raw[:120]}
        except json.JSONDecodeError:
            return {"value": raw[:120]}
    return {}


def _format_hermes_tool_fallback(tool: str, args: dict[str, Any]) -> str | None:
    """Build display text from tool name + args when Hermes sends no label."""
    verb = _HERMES_TOOL_VERBS.get(tool, tool.replace("_", " "))
    if tool == "terminal":
        cmd = args.get("command") or args.get("value", "")
        if cmd:
            cmd = str(cmd).strip()
            if len(cmd) <= 72:
                return f"Ran `{cmd}`"
            return f"Ran `{cmd[:69]}…`"
    if tool == "web_search":
        query = args.get("query", "")
        if query:
            return f"Searched web for `{str(query)[:60]}`"
    if tool == "read_file":
        path = args.get("path", "")
        if path:
            return f"Read `{_basename(str(path))}`"
    if tool == "search_files":
        pattern = args.get("pattern", "")
        if pattern:
            return f"Searched for `{str(pattern)[:40]}`"
    if tool == "execute_code":
        code = args.get("code") or args.get("value", "")
        if code:
            summary = _summarize_code_label(str(code))
            if summary:
                return summary
            return "Ran script"
    if tool == "web_extract":
        urls = args.get("urls") or []
        if urls:
            return f"Fetched `{urls[0][:60]}`"
    return verb.capitalize() if verb else None


def format_hermes_tool_progress(event: dict) -> str | None:
    """Convert a hermes.tool.progress SSE payload to Linear timeline text.

    Hermes runs tools server-side and emits progress on a custom SSE channel
    separate from assistant response tokens. Completed events with output are
    preferred over running start events when a toolCallId is present.
    """
    tool = (event.get("tool") or event.get("name") or "").strip()
    if not tool or tool.startswith(_HERMES_INTERNAL_TOOL_PREFIX):
        return None

    status = (event.get("status") or "running").lower()
    if status not in ("running", "completed", ""):
        return None

    label = (event.get("label") or "").strip()
    args = _parse_hermes_tool_args(event)

    output_raw = event.get("output") or event.get("result")
    if isinstance(output_raw, dict):
        output_text = json.dumps(output_raw, ensure_ascii=False)
    elif isinstance(output_raw, str):
        output_text = output_raw
    else:
        output_text = ""

    if status == "completed" and output_text:
        finding = _extract_finding_from_output(
            tool, output_text, label, args,
        )
        if finding:
            return finding

    if label or args:
        text = _beautify_progress_text(tool, label, args)
    else:
        text = _format_hermes_tool_fallback(tool, args)

    return text


# Opening lines the user already saw as tool-progress on the timeline.
_INTENT_PREAMBLE_STARTS = (
    "let me ", "i'll ", "i will ", "now let me ", "first, let me ",
    "allow me to ", "i need to ", "i should ", "going to ",
    "let's ", "we should ", "time to ",
)


def _strip_intent_preamble(text: str, had_tool_progress: bool) -> str:
    """Drop 'Let me check…' openers when tool work was already shown live."""
    if not had_tool_progress or not text:
        return text

    paragraphs = re.split(r"\n\s*\n", text.strip())
    kept: list[str] = []
    skipping = True
    for para in paragraphs:
        stripped = para.strip()
        if not stripped:
            continue
        if skipping:
            lower = stripped.lower()
            is_intent = any(lower.startswith(s) for s in _INTENT_PREAMBLE_STARTS)
            # Short planning-only paragraph with no finding markers
            has_substance = any(
                marker in lower
                for marker in (
                    "here's", "here is", "the issue", "root cause",
                    "found ", "confirmed", "diagnosis", "summary",
                    "answer:", "result:", "yes,", "no,", "**",
                )
            )
            if is_intent and not has_substance and len(stripped) < 400:
                continue
            skipping = False
        kept.append(stripped)

    result = "\n\n".join(kept).strip()
    return result if result else text


# ── Task Processor ──────────────────────────────────────────────────────


class TaskProcessor:
    """Processes an agent session — analyzes the issue, takes action."""

    def __init__(
        self,
        linear: LinearClient,
        coding: CodingBridge | None = None,
    ) -> None:
        self.linear = linear
        self.coding = coding
        self._viewer_id: str | None = None

    async def ensure_viewer_id(self) -> str:
        if self._viewer_id is None:
            self._viewer_id = await self.linear.get_viewer_id()
        return self._viewer_id


    async def process(
        self, session: AgentSession, issue: dict[str, Any] | None
    ) -> None:
        """Main processing pipeline for a session."""
        session_id = session.session_id
        issue_id = session.issue_id
        log.info(
            "Processing session=%s issue=%s action=%s",
            session_id, session.issue_identifier, session.action.value,
        )

        # 1. Do not emit a visible acknowledgement. Linear only requires an
        # activity OR external URL update within 10s; `update_session` below
        # satisfies that without creating an extra message in the thread.

        try:
            # 2. Fetch full issue if we only have partial data
            if issue is None:
                issue = await self.linear.get_issue(issue_id)

            if not issue:
                await self.linear.send_error(
                    session_id,
                    f"Could not fetch issue {session.issue_identifier}.",
                )
                return

            title = issue.get("title", session.title)
            description = issue.get("description", session.description) or ""
            team_id = issue.get("team", {}).get("id", session.team_id)
            state_type = issue.get("state", {}).get("type", "")
            labels = [l["name"] for l in
                      (issue.get("labels", {}).get("nodes", []) or [])]
            log.info("Issue: %s — %s [%s]", session.issue_identifier, title, state_type)

            # 3. Set self as delegate if not already set (per Linear best practices)
            if issue.get("delegate") is None:
                viewer_id = await self.ensure_viewer_id()
                await self.linear.update_issue(issue_id, delegateId=viewer_id)

            # 4. Auto-move to "In Progress" when picking up task
            if state_type not in ("started", "completed", "canceled") and team_id:
                started_id = await self.linear.find_state_by_type(
                    team_id, "started", preferred_name="In Progress"
                )
                if started_id:
                    await self.linear.update_issue(issue_id, stateId=started_id)

            # 5. Enable agent session with external URL (satisfies 10s ack requirement)
            await self.linear.update_session(
                session_id,
                external_urls=[
                    {"label": "Issue", "url": issue.get("url", "")},
                ],
            )

            # 6. Always route to Hermes LLM reasoning — Hermes has filesystem tools,
            #    terminal access, and everything it needs. Let it decide if coding is needed.
            log.info("Routing to _handle_analysis")
            await self._handle_analysis(session, issue, session_id)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Error processing session %s", session_id)
            await self.linear.send_error(
                session_id,
                f"An error occurred while processing this issue:\n```\n{e}\n```",
            )

    def build_llm_prompt(
        self,
        session: AgentSession,
        issue: dict[str, Any],
        conversation_text: str,
        user_request: str,
        internal_text: str = "",
    ) -> str:
        """Assemble the full LLM prompt from session + issue context.

        The prompt consists of:
        1. System declaration ("You are Hermes...")
        2. LINEAR_API_KEY usage (referenced as env var, never interpolated directly)
        3. Issue context: identifier, title, status, project, team, labels, description
        4. Conversation thread (all comments, chronological, full body — deterministic)
        5. Prior session activity (tool calls, thoughts — prompted sessions only)
        6. User's message (session.body or description fallback)
        7. Instruction: "Do what needs to be done"

        See PLY-32 for the full spec.
        """
        identifier = issue.get("identifier", session.issue_identifier)
        title = issue.get("title", session.title)
        description = issue.get("description", session.description) or ""
        state = issue.get("state", {})
        labels = [l["name"] for l in
                  (issue.get("labels", {}).get("nodes", []) or [])]
        project = issue.get("project") or {}
        project_name = project.get("name", "")
        team = issue.get("team") or {}
        team_name = team.get("name", "")
        team_key = team.get("key", "")

        if session.action == SessionAction.prompted:
            # Follow-up turn — continue the work/conversation in this thread
            return (
                f"You are Hermes, an autonomous agent working on Linear"
                f" issue {identifier} — {title}.\n"
                f"The Hermes API server runs tools (filesystem, shell, web"
                f" search) on your behalf during each request.\n"
                f"\n"
                f"Your LINEAR_API_KEY for GraphQL API calls to api.linear.app"
                f" is available as the environment variable $LINEAR_API_KEY"
                f" — accessible via 'echo $LINEAR_API_KEY' in your shell"
                f" tools if you need it.\n"
                f"\n"
                f"Issue: {identifier} — {title}\n"
                f"Project: {project_name or '(none)'}\n"
                f"Team: {team_name}"
                f"{f' ({team_key})' if team_key else ''}\n"
                f"Labels: {', '.join(labels) or 'none'}\n"
                f"{internal_text}\n"
                f"{conversation_text}\n"
                f"User: {user_request}\n"
                f"\n"
                f"The user already sees each tool action on the Linear"
                f" timeline in real time (reads, searches, shell commands)."
                f" Your final reply is the answer only — not a play-by-play."
                f" Do not open with 'Let me...', 'I'll check...', or other"
                f" planning language. Start directly with findings or a"
                f" direct answer.\n"
                f"\n"
                f"Respond to the new message. If it asks you to do something,"
                f" do it now with your tools and report the result."
                f" If it's casual conversation, just reply naturally."
                f" Do not repeat your previous messages."
            )
        else:
            # User @mentioned Hermes or delegated an issue — do the task
            return (
                f"You are Hermes, an autonomous agent working inside Linear.\n"
                f"The Hermes API server runs tools (filesystem, shell, web"
                f" search) on your behalf during each request.\n"
                f"\n"
                f"Your LINEAR_API_KEY for GraphQL API calls to api.linear.app"
                f" is available as the environment variable $LINEAR_API_KEY"
                f" — accessible via 'echo $LINEAR_API_KEY' in your shell"
                f" tools if you need it.\n"
                f"\n"
                f"Issue: {identifier} — {title}"
                f" | Status: {state.get('name', 'Unknown')}\n"
                f"Project: {project_name or '(none)'}\n"
                f"Team: {team_name}"
                f"{f' ({team_key})' if team_key else ''}\n"
                f"Labels: {', '.join(labels) or 'none'}\n"
                f"Description: {description or '(no description)'}\n"
                f"{internal_text}\n"
                f"{conversation_text}\n"
                f"\n"
                f"User: {user_request}\n"
                f"\n"
                f"The user already sees each tool action on the Linear"
                f" timeline in real time (reads, searches, shell commands)."
                f" Your final reply is the answer only — not a play-by-play."
                f" Do not open with 'Let me...', 'I'll check...', or other"
                f" planning language. Start directly with findings or a"
                f" direct answer.\n"
                f"\n"
                f"Do what needs to be done. Use your tools and report what you"
                f" actually did and found. If it's casual or needs discussion,"
                f" just reply naturally. Do not ask for confirmation before"
                f" starting. Be concise. Do not introduce yourself or list"
                f" capabilities."
            )

    async def _is_coding_task(self, issue: dict[str, Any]) -> bool:
        """Determine if the issue requires coding work.

        Uses keyword heuristics on title, description, and labels.
        Returns False if coding backend is 'none'.
        """
        if settings.coding_agent == "none":
            return False

        title = (issue.get("title") or "").lower()
        description = (issue.get("description") or "").lower()
        labels = [l["name"].lower() for l in
                  (issue.get("labels", {}).get("nodes", []) or [])]

        # Strong signal: explicit coding labels
        coding_labels = {"bug", "feature", "enhancement", "development",
                         "coding", "implementation", "chore"}

        # Title/description keywords suggesting development work
        coding_keywords = [
            "implement", "feature", "bug", "fix", "refactor", "build",
            "code", "develop", "add", "create", "write", "function",
            "method", "class", "api", "endpoint", "route", "migration",
            "test", "testing", "deploy", "config", "setup", "integration",
            "cli", "command", "script", "pipeline", "workflow",
        ]

        text = f"{title} {description}"

        # Check labels first (strongest signal)
        for label in labels:
            if label in coding_labels:
                return True

        # Count keyword matches in title + description
        matches = sum(1 for kw in coding_keywords if kw in text)

        # Coding task if 2+ keyword matches, or 1 match in title
        if matches >= 2:
            return True
        if matches >= 1 and any(kw in title for kw in coding_keywords):
            return True

        return False

    async def _create_child_issue(
        self,
        team_id: str,
        parent_id: str,
        title: str,
        description: str = "",
    ) -> dict[str, Any] | None:
        """Create a child issue in the same team, linked to the parent."""
        if not team_id or not parent_id:
            log.warning("Cannot create child issue: missing team_id or parent_id")
            return None
        try:
            data = await self.linear._gql(GQL_ISSUE_CREATE, {
                "input": {
                    "teamId": team_id,
                    "title": title,
                    "description": description,
                    "parentId": parent_id,
                },
            })
            result = data.get("issueCreate", {})
            if result.get("success"):
                issue_data = result.get("issue", {})
                log.info("Created child issue: %s", issue_data.get("identifier", ""))
                return issue_data
            else:
                log.warning("Failed to create child issue")
                return None
        except Exception as e:
            log.exception("Error creating child issue: %s", e)
            return None

    async def _handle_dev_task(
        self,
        session: AgentSession,
        issue: dict[str, Any],
        session_id: str,
    ) -> None:
        """Route to coding agent (Claude Code / Codex).

        Full subagent delegation flow:
        1. Create child issue to track subagent work
        2. Include user's message as custom subagent instructions
        3. Run the coding agent (supports parallel backends)
        4. Post results as comment on parent issue
        5. Move parent to In Review (review-before-merge)
        6. Update child issue status
        """
        assert self.coding is not None

        title = issue.get("title", session.title)
        description = issue.get("description", session.description) or ""
        identifier = issue.get("identifier", session.issue_identifier)
        team_id = issue.get("team", {}).get("id", session.team_id)
        issue_id = issue.get("id", "")

        # 1. Create child issue to track subagent work
        child_issue = await self._create_child_issue(
            team_id=team_id,
            parent_id=issue_id,
            title=f"[Subagent] {title}",
            description=(
                f"Automatically created sub-issue for coding agent"
                f" ({settings.coding_agent}).\n\n"
                f"Parent: {identifier}\n"
                f"Description: {description[:500]}"
            ),
        )

        child_ref = ""
        if child_issue:
            child_ref = child_issue.get("identifier", "")
            log.info("Created child issue %s for subagent work", child_ref)

        await self.linear.send_action(
            session_id,
            "Delegating to coding agent",
            identifier,
            f"Spinning up **{settings.coding_agent.title()} Code** to work on "
            f"**{identifier}**...\n"
            f"{'Child issue: ' + child_ref if child_ref else ''}",
        )

        # 2. Include user's message as custom subagent instructions
        custom_instructions = session.body or ""
        context_parts = [f"Issue: {identifier}"]
        if session.labels:
            context_parts.append(f"Labels: {', '.join(session.labels)}")
        if custom_instructions:
            context_parts.append(f"Custom instructions: {custom_instructions}")
        context = "\n".join(context_parts)

        # 3. Run the coding agent
        result = await self.coding.run(
            issue_id=issue_id,
            title=title,
            description=description,
            context=context,
        )

        if result["success"] and result["output"]:
            output = result["output"]
            if len(output) > 15000:
                output = output[:15000] + "\n\n*(output truncated)*"

            # 4. Post results as comment on parent issue
            comment_body = (
                f"**{settings.coding_agent.title()} Code** completed work on "
                f"**{identifier}**:\n\n```\n{output[:3000]}\n```\n\n"
                f"*(Full output available in agent session)*"
            )
            if child_ref:
                comment_body += f"\nReview child issue: {child_ref}"
            await self.linear.comment(issue_id, comment_body)

            await self.linear.send_action(
                session_id,
                "Completed",
                identifier,
                result=f"```\n{output[:2000]}\n```",
            )

            # 5. Move parent to In Review (review-before-merge)
            review_id = None
            if team_id:
                review_id = await self.linear.find_state_by_type(
                    team_id, "started", preferred_name="In Review"
                )
                if review_id:
                    await self.linear.update_issue(issue_id, stateId=review_id)

            # 6. Update child issue status
            if child_issue and review_id:
                await self.linear.update_issue(child_issue["id"], stateId=review_id)

            await self.linear.send_response(
                session_id,
                f"**Done!** {settings.coding_agent.title()} Code finished "
                f"working on **{identifier}**.\n"
                f"{'Review changes in child issue: ' + child_ref if child_ref else ''}",
            )

        elif result["error"]:
            await self.linear.send_error(
                session_id,
                f"Coding agent failed for **{identifier}**:\n```\n{result['error']}\n```",
            )
            if child_issue:
                await self.linear.comment(
                    child_issue["id"],
                    f"Coding agent failed.\n```\n{result['error'][:2000]}\n```",
                )

    async def _handle_analysis(
        self,
        session: AgentSession,
        issue: dict[str, Any],
        session_id: str,
    ) -> None:
        """Non-coding task: use LLM to reason and respond intelligently."""
        identifier = issue.get("identifier", session.issue_identifier)
        description = issue.get("description", session.description) or ""
        title = issue.get("title", session.title) or ""

        # ── Deterministic conversation reconstruction ──
        # Always show ALL issue comments chronologically (full body, no truncation).
        # This is deterministic — survives provider failures, session resets, and
        # stale activities. The LLM never has to guess what was said earlier.
        conversation_text = ""
        raw_nodes = issue.get("comments", {}).get("nodes", []) or []
        flat = _flatten_comments(raw_nodes)
        if flat:
            # Sort ALL comments chronologically for a coherent conversation view
            flat.sort(key=lambda item: item[1].get("createdAt", ""))
            conversation_text = "\n\nFull conversation (all comments, chronological):\n"
            for depth, c in flat:
                conversation_text += _format_comment_line(depth, c) + "\n"

        # For prompted sessions, ALSO include session activities (tool calls,
        # thoughts, internal reasoning) as supplementary context above the comments.
        internal_text = ""
        if session.action == SessionAction.prompted:
            activities = await self.linear.get_session_activities(session_id)
            if activities:
                internal_text = _format_activities_conversation(activities)
                if internal_text:
                    internal_text = "\n\nPrior session activity (Hermes internal):\n" + internal_text

        # The user's actual request (from the comment that triggered this)
        user_request = session.body or description or f"Respond to issue {identifier}"

        # Build the full prompt using the extracted method
        prompt = self.build_llm_prompt(
            session, issue, conversation_text, user_request, internal_text,
        )

        # Initialize DiscoveryTracker for result-oriented progress updates
        tracker = DiscoveryTracker(session_id=session_id, linear=self.linear)
        # Set initial context — cursor-style: "Examining issue PLY-41"
        await tracker.in_progress(f"Examining issue {identifier}")

        # Start background keepalive to continue sending activity during long LLM calls
        keepalive_task = asyncio.create_task(
            self._keep_session_alive(session_id, tracker)
        )

        try:
            # Call LLM for reasoning with discovery extraction
            response_text = await self._call_llm(prompt, session_id, tracker)
        finally:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

        if response_text:
            # Flush pending thought activities so all intermediate progress
            # updates are visible in Linear before the final response.
            if tracker:
                await tracker.flush()
                response_text = _strip_intent_preamble(
                    response_text,
                    tracker.tool_progress_count > 0,
                )

            await self.linear.send_response(session_id, response_text)
            # Task complete — move to "In Review" for the user
            if session.action != SessionAction.prompted:
                team_id_local = issue.get("team", {}).get("id", session.team_id)
                issue_id_local = issue.get("id", "")
                if team_id_local and issue_id_local:
                    review_id = await self.linear.find_state_by_type(
                        team_id_local, "started", preferred_name="In Review"
                    )
                    if review_id:
                        await self.linear.update_issue(issue_id_local, stateId=review_id)
        else:
            await self.linear.send_response(
                session_id,
                f"Could not generate a response. Status: {issue.get('state', {}).get('name', 'Unknown')}",
            )

    async def _call_llm(
        self, prompt: str, session_id: str = "",
        tracker: DiscoveryTracker | None = None,
    ) -> str | None:
        """Call Hermes API server for reasoning with streaming support.

        Parses Hermes' custom ``hermes.tool.progress`` SSE events for
        DiscoveryTracker timeline updates. Response text is accumulated
        separately and never streamed to Linear (avoids duplication).
        Retries once (5s backoff) on timeout or 5xx responses.
        """
        if not settings.hermes_api_key:
            log.warning("HERMES_API_KEY not set — cannot call LLM")
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
                if session_id:
                    headers["X-Hermes-Session-Id"] = session_id

                async with httpx.AsyncClient(timeout=600.0) as client:
                    async with client.stream(
                        "POST",
                        f"{settings.hermes_api_url}/chat/completions",
                        headers=headers,
                        json={
                            "model": settings.hermes_model,
                            "messages": [
                                {"role": "user", "content": prompt}
                            ],
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
                        seen_repos: set[str] = set()

                        def _should_emit_progress(text: str) -> bool:
                            key = _normalize_progress_markdown(text).lower()
                            if key in seen_progress:
                                return False
                            if text.startswith("Working in `"):
                                repo = text[12:].rstrip("`")
                                if repo in seen_repos:
                                    return False
                                seen_repos.add(repo)
                            seen_progress.add(key)
                            seen_progress.add(text)
                            return True

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
                                discovery = format_hermes_tool_progress(
                                    progress_data,
                                )
                                if (
                                    discovery
                                    and tracker
                                    and progress_worker
                                    and _should_emit_progress(discovery)
                                ):
                                    seen_progress.add(discovery)
                                    tracker._keepalive_ctx = discovery[:100]
                                    tracker._skip_texts.add(discovery)
                                    tracker.tool_progress_count += 1
                                    await progress_worker.put(discovery)
                                    log.info(
                                        "Hermes tool progress: %s",
                                        discovery[:80],
                                    )
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
                                session_id
                                and not content
                                and now - last_drought_time > 5.0
                            ):
                                if tracker:
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
                log.warning(
                    "Hermes API call timed out after %.2fs (attempt %d/2)",
                    elapsed, attempt + 1,
                )
                if attempt == 0:
                    log.info("Retrying in 5s...")
                    # Stop current worker before retry (a new one starts next iteration)
                    if progress_worker:
                        await progress_worker.drain_and_stop()
                    await asyncio.sleep(5)
                    continue
                if progress_worker:
                    await progress_worker.drain_and_stop()
                return None

            except Exception as e:
                elapsed = time.monotonic() - start
                log.warning(
                    "Hermes API call failed after %.2fs (attempt %d/2): %s",
                    elapsed, attempt + 1, e,
                )
                if attempt == 0:
                    log.info("Retrying in 5s...")
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

    async def _keep_session_alive(self, session_id: str, tracker: DiscoveryTracker | None = None) -> None:
        """Periodically emit ephemeral thoughts to keep the session alive
        during long LLM processing. Uses tracker context when available."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            try:
                ctx = tracker.keepalive_context() if tracker else "Working on it..."
                await self.linear.create_activity(
                    session_id,
                    ActivityType.thought,
                    body=ctx,
                    ephemeral=True,
                )
                log.debug("Keepalive thought sent for session %s", session_id)
            except Exception:
                log.warning("Keepalive activity failed for session %s", session_id)


# ── Rate Limiter ──────────────────────────────────────────────────────────


class SlidingWindowRateLimiter:
    """Sliding-window rate limiter.

    Tracks request timestamps in a deque and rejects requests that exceed
    ``max_requests`` within any ``window_s``-second window.
    """

    def __init__(self, max_requests: int, window_s: float) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self._timestamps: deque[float] = deque()

    def allow(self) -> bool:
        """Check if a request is allowed under the rate limit.

        Returns True and records the timestamp if under the limit.
        Returns False if the limit is exceeded (caller should return 429).
        """
        now = time.time()
        cutoff = now - self.window_s

        # Prune expired timestamps from the left
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

        if len(self._timestamps) >= self.max_requests:
            return False

        self._timestamps.append(now)
        return True

    @property
    def current_count(self) -> int:
        """Number of requests in the current window."""
        now = time.time()
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        return len(self._timestamps)


# ── Webhook Handler ──────────────────────────────────────────────────────


class AgentWebhookHandler:
    """Processes incoming Linear webhooks and manages agent sessions."""

    def __init__(
        self, linear: LinearClient, processor: TaskProcessor
    ) -> None:
        self.linear = linear
        self.processor = processor
        self._active_runs: dict[str, asyncio.Task[None]] = {}
        self._dedup_cache: dict[str, float] = {}
        # Rate limiting
        self._concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)
        self._rate_limiter = SlidingWindowRateLimiter(
            max_requests=RATE_LIMIT_MAX_REQUESTS,
            window_s=RATE_LIMIT_WINDOW_S,
        )

    def _check_dedup(self, key: str, ttl: float = 60.0) -> bool:
        """Returns True if this event was recently processed."""
        now = time.time()
        # Prune stale entries periodically
        if now % 100 < 1:
            self._dedup_cache = {
                k: v for k, v in self._dedup_cache.items()
                if now - v < 120
            }
        if key in self._dedup_cache and now - self._dedup_cache[key] < ttl:
            return True  # Duplicate
        self._dedup_cache[key] = now
        return False

    async def _is_self_comment(self, payload: dict[str, Any]) -> bool:
        """Check if the webhook event was triggered by the agent itself.

        Compares the event's actor/user ID against the agent's own viewer ID
        to prevent self-looping. Previously this returned True for ANY non-empty
        actorId/appUserId, which classified all human comments as self-comments.
        """
        try:
            actor_id = payload.get("notification", {}).get("actorId", "")
            app_user_id = payload.get("appUserId", "")
            user_id = actor_id or app_user_id
            if not user_id:
                return False
            viewer_id = await self.processor.ensure_viewer_id()
            return user_id == viewer_id
        except Exception:
            return False

    async def handle_event(
        self, payload: dict[str, Any]
    ) -> str:
        """Route a webhook event to the appropriate handler.

        Returns a status message for logging.
        """
        event_type = payload.get("type", "")
        action = payload.get("action", "")

        log.info("Webhook: type=%s action=%s", event_type, action)

        # Debug: dump full payload structure (remove after fixing)
        log.info("Full webhook payload keys: %s", json.dumps(list(payload.keys())))
        # Log the top-level structure with sizes (to avoid massive payloads in logs)
        def summarize(obj, depth=0):
            if depth > 3:
                return "..."
            if isinstance(obj, dict):
                return {k: summarize(v, depth+1) for k, v in obj.items()}
            elif isinstance(obj, list):
                return f"[list len={len(obj)}]" if len(obj) > 3 else [summarize(v, depth+1) for v in obj]
            elif isinstance(obj, str):
                return f"str({len(obj)} chars)"
            else:
                return obj
        log.info("Payload structure: %s", json.dumps(summarize(payload), indent=2, default=str))

        # ── Agent Session Events ──
        if event_type == "AgentSessionEvent" and action in ("created", "prompted"):
            return await self._handle_agent_session(payload, SessionAction(action))

        # ── Comment Events (for @mentions that may not trigger agent sessions) ──
        if event_type == "Comment" and action == "create":
            if await self._is_self_comment(payload):
                log.info("Skipping own comment — self-loop prevention")
                return "skipped (self-comment)"
            return await self._handle_comment(payload)

        # ── Issue Events ──
        if event_type == "Issue" and action == "update":
            return await self._handle_issue_update(payload)

        log.info("Unhandled event type=%s action=%s", event_type, action)
        return f"unhandled ({event_type}/{action})"

    async def _handle_agent_session(
        self, payload: dict[str, Any], action: SessionAction
    ) -> str:
        """Handle AgentSessionEvent.created or .prompted."""
        # AgentSessionEvent: data is at top level
        agent_session = payload.get("agentSession", {})
        session_id = agent_session.get("id", "")
        # promptContext and agentActivity are also at top level
        prompt_context_raw = payload.get("promptContext", "")
        agent_activity = payload.get("agentActivity", {})

        if not session_id:
            return "ignored (no session id)"

        # Build our session object first so we can read signal from agent_activity
        parsed_context = parse_prompt_context(prompt_context_raw)

        issue_data = agent_session.get("issue", {})
        comment_data = agent_session.get("comment", {})
        comment_body = comment_data.get("body", "")
        previous_comments = payload.get("previousComments", [])
        agent_activity = payload.get("agentActivity", {})
        activity_body = agent_activity.get("body", "")

        # Linear auto-creates a session comment like "This thread is for an agent
        # session with Hermes." when an issue is delegated (no real @mention). That
        # is a SYSTEM message, not user input — discard it so the real task (the
        # issue description) is used instead.
        if _is_linear_system_comment(comment_body):
            comment_body = ""

        # For prompted events, the new user message is in agentActivity.body.
        # For created events, only a real @mention comment counts as user content.
        if action == SessionAction.prompted:
            user_body = activity_body or ""
        else:
            user_body = comment_body or ""

        # Dedup — for prompted events key on the activity ID (allows multiple
        # distinct follow-ups while still dropping duplicate webhook fires).
        if action == SessionAction.prompted:
            activity_id = agent_activity.get("id", "")
            dedup_key = f"activity:{activity_id}" if activity_id else f"session:{session_id}:prompted"
        else:
            dedup_key = f"session:{session_id}:{action.value}"
        if self._check_dedup(dedup_key):
            log.info("Dedup hit for session %s", session_id)
            return "deduped"

        # Handle stop signal — cancel running task, do not spawn a new one
        if agent_activity.get("signal") == "stop":
            existing = self._active_runs.get(session_id)
            if existing and not existing.done():
                existing.cancel()
                log.info("Stop signal received; cancelling session %s", session_id[:8])
                await self.linear.send_response(
                    session_id,
                    "Stopped. Work has been halted as requested.",
                )
                return "stopped (stop signal — task cancelled)"
            await self.linear.send_response(
                session_id,
                "No active task to stop.",
            )
            return "stopped (stop signal — no active task)"

        # Check if already running
        existing = self._active_runs.get(session_id)
        if existing and not existing.done():
            log.info("Session %s already active", session_id)
            return "already running"

        session = AgentSession(
            session_id=session_id,
            issue_id=issue_data.get("id", ""),
            issue_identifier=issue_data.get("identifier",
                                            parsed_context.get("identifier", "")),
            action=action,
            prompt_context=prompt_context_raw,
            body=user_body,
            original_body=comment_body if action == SessionAction.prompted else "",
            title=issue_data.get("title", parsed_context.get("title", "")),
            description=issue_data.get("description",
                                        parsed_context.get("description", "")),
            team_id=issue_data.get("team", {}).get("id", ""),
            team_key=issue_data.get("team", {}).get("key", ""),
            team_name=parsed_context.get("team_name", ""),
            priority=issue_data.get("priority", 0),
            labels=parsed_context.get("labels", []),
            state_name=issue_data.get("state", {}).get("name", ""),
            state_type=issue_data.get("state", {}).get("type", ""),
            comments=previous_comments,
            guidance=parsed_context.get("guidance", []),
        )

        # ── Team allowlist check ──
        if settings.allowed_team_ids and session.team_id not in settings.allowed_team_ids:
            log.info(
                "Session %s from team %s not in allowlist — ignoring",
                session_id[:8], session.team_id,
            )
            return "ignored (team not in allowlist)"

        # Spawn background processing and track the task for cancellation
        task: asyncio.Task[None] = asyncio.create_task(self._run_session(session))
        self._active_runs[session_id] = task
        return f"processing (session={session_id[:8]}...)"

    async def _run_session(self, session: AgentSession) -> None:
        """Background task for an agent session."""
        async with self._concurrency_semaphore:
            try:
                issue = await self.linear.get_issue(session.issue_id)
                await self.processor.process(session, issue)
            except asyncio.CancelledError:
                log.info("Session %s cancelled via stop signal", session.session_id[:8])
                raise
            except Exception as e:
                log.exception("Session %s crashed", session.session_id)
                try:
                    await self.linear.send_error(
                        session.session_id,
                        f"Internal error: {e}",
                    )
                except Exception:
                    log.exception("Failed to send error activity")
            finally:
                self._active_runs.pop(session.session_id, None)
                log.info("Session %s complete", session.session_id[:8])

    async def _process_with_semaphore(
        self, session: AgentSession, issue: dict[str, Any] | None
    ) -> None:
        """Wrap processor.process in the concurrency semaphore.

        Used by _handle_comment and _handle_issue_update which spawn
        processing directly without going through _run_session.
        """
        async with self._concurrency_semaphore:
            await self.processor.process(session, issue)

    async def _handle_comment(self, payload: dict[str, Any]) -> str:
        """Handle @mentions in comments."""
        # AppUserNotification: data is in notification object
        notif = payload.get("notification", {})
        comment_body = notif.get("comment", {}).get("body", "")
        issue_id = notif.get("issueId", "")
        comment_id = notif.get("commentId", "")

        # Check for @Hermes mention
        bot_name = settings.linear_agent_bot_name.lower()
        if f"@{bot_name}" not in comment_body.lower():
            return "ignored (no mention)"

        dedup_key = f"comment:{comment_id}"
        if self._check_dedup(dedup_key):
            return "deduped"

        # Create a proactive agent session on the issue
        log.info("Creating agent session for @mention on issue %s", issue_id)
        try:
            data = await self.linear._gql(GQL_CREATE_SESSION_ON_ISSUE, {
                "input": {"issueId": issue_id},
            })
            session_data = data.get("agentSessionCreateOnIssue", {})
            if session_data.get("success"):
                session_id = session_data["agentSession"]["id"]
                # Build a basic session
                issue = await self.linear.get_issue(issue_id)
                if issue:
                    # ── Team allowlist check ──
                    team_id_comment = issue.get("team", {}).get("id", "")
                    if settings.allowed_team_ids and team_id_comment not in settings.allowed_team_ids:
                        log.info(
                            "@mention on issue %s from team %s not in allowlist — ignoring",
                            issue_id, team_id_comment,
                        )
                        return "ignored (team not in allowlist)"

                    session = AgentSession(
                        session_id=session_id,
                        issue_id=issue_id,
                        issue_identifier=issue.get("identifier", ""),
                        action=SessionAction.created,
                        prompt_context="",
                        title=issue.get("title", ""),
                        description=issue.get("description", "") or "",
                        team_id=issue.get("team", {}).get("id", ""),
                        team_key=issue.get("team", {}).get("key", ""),
                        priority=issue.get("priority", 0),
                        labels=[l["name"] for l in
                                (issue.get("labels", {}).get("nodes", []) or [])],
                        comments=issue.get("comments", {}).get("nodes", []),
                    )
                    asyncio.create_task(self._process_with_semaphore(session, issue))
                    return f"processing @mention (session={session_id[:8]}...)"
        except Exception as e:
            log.exception("Failed to create agent session for @mention")
            return f"error: {e}"

        return "no action"

    async def _handle_issue_update(self, payload: dict[str, Any]) -> str:
        """Handle issue assignments or delegations to the agent."""
        # Issue webhook: data is at top level
        assignee_id = payload.get("assigneeId", "") or payload.get("issue", {}).get("assigneeId", "")
        delegate_id = payload.get("delegateId", "") or payload.get("issue", {}).get("delegateId", "")
        issue_id = payload.get("issueId", "") or payload.get("id", "")

        # Check if the issue was assigned or delegated to us
        try:
            viewer_id = await self.processor.ensure_viewer_id()
            if assignee_id != viewer_id and delegate_id != viewer_id:
                return "not assigned/delegated to agent"
        except Exception:
            return "could not check assignment"

        log.info("Issue %s assigned to agent", issue_id)
        # Create an agent session proactively
        try:
            data = await self.linear._gql(GQL_CREATE_SESSION_ON_ISSUE, {
                "input": {"issueId": issue_id},
            })
            session_data = data.get("agentSessionCreateOnIssue", {})
            if session_data.get("success"):
                session_id = session_data["agentSession"]["id"]
                issue = await self.linear.get_issue(issue_id)
                # ── Team allowlist check ──
                if issue:
                    team_id_update = issue.get("team", {}).get("id", "")
                    if settings.allowed_team_ids and team_id_update not in settings.allowed_team_ids:
                        log.info(
                            "Assignment on issue %s from team %s not in allowlist — ignoring",
                            issue_id, team_id_update,
                        )
                        return "ignored (team not in allowlist)"
                if issue:
                    session = AgentSession(
                        session_id=session_id,
                        issue_id=issue_id,
                        issue_identifier=issue.get("identifier", ""),
                        action=SessionAction.created,
                        prompt_context="",
                        title=issue.get("title", ""),
                        description=issue.get("description", "") or "",
                        team_id=issue.get("team", {}).get("id", ""),
                        team_key=issue.get("team", {}).get("key", ""),
                        priority=issue.get("priority", 0),
                        labels=[l["name"] for l in
                                (issue.get("labels", {}).get("nodes", []) or [])],
                        state_type=issue.get("state", {}).get("type", ""),
                    )
                    asyncio.create_task(self._process_with_semaphore(session, issue))
                    return f"processing assignment (session={session_id[:8]}...)"
        except Exception as e:
            log.exception("Failed to handle assignment")
            return f"error: {e}"

        return "no action"


# ── FastAPI App ─────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan — set up and tear down clients."""
    log.info(
        "═══════════════════════════════════════════════\n"
        "  Linear Agent starting...\n"
        "  Port: %s\n"
        "  Backend: %s\n"
        "  Workdir: %s\n"
        "═══════════════════════════════════════════════",
        PORT, settings.coding_agent, settings.coding_workdir,
    )

    # Create shared clients
    linear = LinearClient(settings.linear_api_key)
    coding = CodingBridge(settings.coding_agent, settings.coding_workdir)
    processor = TaskProcessor(linear, coding)
    handler = AgentWebhookHandler(linear, processor)

    # Stash on app
    app.state.linear = linear
    app.state.handler = handler

    # Verify API connectivity
    try:
        viewer = await linear.get_viewer()
        log.info("Authenticated as: %s (%s)", viewer["name"], viewer["email"])
    except Exception as e:
        log.warning("API auth check failed (will retry): %s", e)

    yield

    # Shutdown
    await linear.close()
    log.info("Linear agent shut down.")


app = FastAPI(
    title="Linear Agent",
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
        "agent": "linear-agent",
        "backend": settings.coding_agent,
    }


@app.post("/linear/webhook")
@app.post("/webhook/linear")
async def linear_webhook(request: Request) -> Response:
    """Receive Linear webhook events (AgentSessionEvent, Comment, Issue)."""
    # 1. Verify IP (optional but recommended)
    if settings.linear_enforce_ip_allowlist and not verify_ip(request):
        log.warning("Request from untrusted IP: %s", request.client)
        raise HTTPException(status_code=403, detail="Untrusted IP")

    # 2. Read body
    body = await request.body()

    # 3. Verify HMAC signature
    signature = request.headers.get("Linear-Signature", "")
    if not verify_hmac(body, signature, settings.linear_webhook_secret):
        log.warning("HMAC verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 4. Check rate limit
    handler: AgentWebhookHandler = request.app.state.handler
    if not handler._rate_limiter.allow():
        retry_after = int(RATE_LIMIT_WINDOW_S)
        log.warning(
            "Rate limit exceeded (%d/%d) — returning 429",
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

    # 5. Parse payload
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 6. Route event (background, HTTP 200 fast)
    status = await handler.handle_event(payload)
    log.info("Event handled: %s", status)

    return Response(
        content=json.dumps({"status": status}),
        media_type="application/json",
        status_code=200,
    )


# ── Main ─────────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point."""
    import uvicorn
    uvicorn.run(
        "linear_agent:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
