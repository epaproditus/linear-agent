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
import socket
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

# ── Hermes response style (Cursor user rules → Linear/Hermes) ──────────────

HERMES_WORK_STYLE = """
Work style:
- Context before action: read the project overview, issue description, full
  comment thread, sibling project issues, and workspace guidance in this prompt
  before running shell commands, installing packages, or changing system config
  (SSH, PAM, firewall, users, services). Identify the target host (VPS, agent
  host, or other), environment, and paths from that text — do not guess.
- Disambiguate before deep diagnosis: when a user names a product, service, repo,
  or UI that could mean more than one thing (e.g. "dashboard" vs "webui" vs
  "desktop app" vs a systemd unit), ask one short clarifying question before
  running extensive shell probes. Do not assume which variant they mean.
- Batch shell diagnostics: combine related checks in one terminal script per
  round (service status + ports + recent logs together) instead of many separate
  one-command calls. Fewer round-trips, same coverage.
- During tool loops keep assistant text minimal — no "Let me check…", "Found it!",
  or step-by-step narration. Tool progress appears on the Linear timeline; save
  findings for the final reply.
- Execution target: shell and filesystem tools run on the agent host named in
  this prompt unless you explicitly SSH elsewhere. Remote work requires evidence
  in the thread (hostname, IP, SSH alias) and an explicit ssh command.
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
Reply style (Linear issue comment — user already saw tool progress on the timeline):
- Write like a clear technical post: complete sentences, plain language, no jargon padding.
- Match depth to the question — short questions get short answers.
- Open with the finding, answer, or decision — not setup or process narration.
- Reference existing code with citation fences: ```startLine:endLine:filepath (Cursor/Linear format).
- For code changes: include the GitHub PR URL in your reply. Linear shows the full diff in its Reviews UI — do not paste large patches or ```diff blocks in chat.
- For complex logic or architecture, include a ```mermaid diagram when it clarifies the flow.
- Also use short paragraphs, bullets, or numbered steps where helpful.
- Use markdown sparingly; full URLs for links. Do not over-bold or over-backtick.
- No filler endings ("let me know if…", "happy to help", "say the word").
- Preserve every fact, recommendation, and code change from the draft.
""".strip()

# Linear agent session plan step statuses (Agent Plans API).
_PLAN_PENDING = "pending"
_PLAN_ACTIVE = "inProgress"
_PLAN_DONE = "completed"
PLAN_STEP_MAX_LEN = 48
PLAN_MAX_STEPS = 5
PLAN_STEP_MAX_WORDS = 6
PROJECT_CONTEXT_MAX_LEN = 4000
PROJECT_SIBLING_ISSUES_MAX = 8
PROJECT_SIBLING_DESC_MAX = 200
CONVERSATION_SUMMARY_MAX = 1500

LINEAR_OUTPUT_RULES = """
Linear output rules:
- The user already sees tool progress on the issue timeline.
- Your final message here should be conclusions, findings, and decisions only.
- No process narration ("I checked…", "Let me…", "Next I'll…") — the timeline
  already showed what you did.
- For code changes: include the GitHub PR URL. Do not paste large diffs.
- Reference existing code with ```startLine:endLine:filepath citations when helpful.
""".strip()

HERMES_NATIVE_TODO_HINT = (
    "For multi-step work, use the todo tool to track steps."
)

GATE_ISSUE_HINT = """
Human gate issue — help Abraham decide, do not execute:
- Recommend options, parameters, and findings only.
- Read-only investigation is fine (logs, docs, data).
- Do not deploy, change production systems, open PRs, or use delegate_task.
- Summarize choices for Abraham to confirm in a comment.
""".strip()

WATERMARK_STORE_DIR = Path.home() / ".linear-agent"
WATERMARK_STORE_PATH = WATERMARK_STORE_DIR / "conversation_watermarks.json"

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

    agent_workdir: str = str(Path.home() / "linear-agent" / "workspace")
    """Default working directory for Hermes shell/filesystem tools."""

    hermes_native_mode: bool = False
    """Thin Hermes-native adapter: one session, todo→plan, no synthetic planning."""

    linear_defer_on_blockers: bool = True
    """Refuse new work on created turns when blocked by unfinished issues."""

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
    project {
      id
      name
      description
      content
      url
      status { name type }
    }
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
    relations {
      nodes {
        type
        relatedIssue {
          id
          identifier
          title
          state { name type }
        }
      }
    }
    inverseRelations {
      nodes {
        type
        issue {
          id
          identifier
          title
          state { name type }
        }
      }
    }
  }
}
"""

GQL_PROJECT_ISSUES = """
query ProjectIssues($projectId: ID!, $first: Int!) {
  issues(
    filter: { project: { id: { eq: $projectId } } }
    first: $first
    orderBy: updatedAt
  ) {
    nodes {
      id
      identifier
      title
      description
      updatedAt
      state { name }
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
    project_name: str = ""
    project_summary: str = ""
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

    async def get_project_issue_summaries(
        self,
        project_id: str,
        exclude_issue_id: str = "",
        limit: int = PROJECT_SIBLING_ISSUES_MAX,
    ) -> list[dict[str, Any]]:
        """Recent issues in the same project (for cross-issue context)."""
        if not project_id:
            return []
        try:
            data = await self._gql(
                GQL_PROJECT_ISSUES,
                {"projectId": project_id, "first": limit + 4},
            )
            nodes = data.get("issues", {}).get("nodes", []) or []
            out: list[dict[str, Any]] = []
            for node in nodes:
                if exclude_issue_id and node.get("id") == exclude_issue_id:
                    continue
                out.append(node)
                if len(out) >= limit:
                    break
            return out
        except RuntimeError:
            log.debug("Could not fetch project issues for %s", project_id[:8])
            return []

    async def fetch_hermes_todos(self, hermes_session_id: str) -> list[dict[str, Any]]:
        """Read Hermes native todo list for a session (GET /api/todos/{id})."""
        if not settings.hermes_api_key or not hermes_session_id:
            return []
        base = settings.hermes_api_url.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        url = f"{base}/api/todos/{hermes_session_id}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {settings.hermes_api_key}"},
                )
                if resp.status_code == 404:
                    return []
                if resp.status_code != 200:
                    log.debug("Hermes todos API returned %d", resp.status_code)
                    return []
                data = resp.json()
                if isinstance(data, dict):
                    todos = data.get("todos")
                    if isinstance(todos, list):
                        return [t for t in todos if isinstance(t, dict)]
                if isinstance(data, list):
                    return [t for t in data if isinstance(t, dict)]
        except Exception:
            log.debug("Could not fetch Hermes todos", exc_info=True)
        return []
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
        added_external_urls: list[dict[str, str]] | None = None,
        summary: str | None = None,
    ) -> bool:
        """Update session metadata."""
        inp: dict[str, Any] = {}
        if external_urls is not None:
            inp["externalUrls"] = [{"label": u["label"], "url": u["url"]}
                                   for u in external_urls]
        if added_external_urls is not None:
            inp["addedExternalUrls"] = [
                {"label": u["label"], "url": u["url"]}
                for u in added_external_urls
            ]
        if summary is not None:
            inp["summary"] = summary
        data = await self._gql(GQL_UPDATE_SESSION, {
            "id": session_id,
            "input": inp,
        })
        return data.get("agentSessionUpdate", {}).get("success", False)


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

    m = re.search(
        r'<project name="([^"]*)"[^>]*>([^<]*)</project>', xml_str,
    )
    if m:
        result["project_name"] = m.group(1).strip()
        result["project_summary"] = m.group(2).strip()
    else:
        m = re.search(r'<project name="([^"]*)"', xml_str)
        if m:
            result["project_name"] = m.group(1).strip()

    return result


def _truncate_context(text: str, limit: int = PROJECT_CONTEXT_MAX_LEN) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(truncated)"


def format_project_context_block(
    issue_project: dict[str, Any] | None,
    *,
    fallback_name: str = "",
    fallback_summary: str = "",
) -> str:
    """Format Linear project fields for LLM prompt injection."""
    project = issue_project or {}
    name = (project.get("name") or fallback_name or "").strip()
    summary = (project.get("description") or fallback_summary or "").strip()
    content = (project.get("content") or "").strip()

    if not name and not summary and not content:
        return ""

    lines = [f"Project: {name or '(unnamed)'}"]

    status = project.get("status") or {}
    status_name = (status.get("name") or "").strip()
    status_type = (status.get("type") or "").strip()
    if status_name or status_type:
        if status_name and status_type:
            lines.append(f"Project status: {status_name} ({status_type})")
        else:
            lines.append(f"Project status: {status_name or status_type}")

    url = (project.get("url") or "").strip()
    if url:
        lines.append(f"Project URL: {url}")

    if summary:
        lines.append(f"Project summary: {summary}")

    if content:
        lines.append(f"Project overview:\n{_truncate_context(content)}")

    return "\n".join(lines) + "\n"


def format_guidance_block(guidance: list[str]) -> str:
    """Format workspace/team guidance rules for LLM prompt injection."""
    rules = [g.strip() for g in guidance if g and g.strip()]
    if not rules:
        return ""
    return (
        "Team/workspace guidance:\n"
        + "\n".join(f"- {rule}" for rule in rules)
        + "\n"
    )



def format_execution_environment_block() -> str:
    """Tell the LLM where shell/filesystem tools actually run."""
    host = socket.gethostname()
    workdir = settings.agent_workdir
    return (
        "Execution environment (where shell/filesystem tools run by default):\n"
        f"- Agent host: {host}\n"
        f"- Default working directory: {workdir}\n"
        "- Commands affect this host unless you explicitly SSH to another.\n"
        "- Confirm the intended target (VPS, staging, prod) from project "
        "overview, comments, or sibling issues before system changes.\n"
    )


def format_project_issues_block(issues: list[dict[str, Any]]) -> str:
    """Summarize recent sibling issues in the same Linear project."""
    if not issues:
        return ""
    lines = [
        "Recent issues in this project (shared context — review before acting):",
    ]
    for iss in issues[:PROJECT_SIBLING_ISSUES_MAX]:
        ident = iss.get("identifier", "")
        title = (iss.get("title") or "").strip()
        state = (iss.get("state") or {}).get("name", "")
        desc = (iss.get("description") or "").replace("\n", " ").strip()
        if len(desc) > PROJECT_SIBLING_DESC_MAX:
            desc = desc[:PROJECT_SIBLING_DESC_MAX] + "…"
        state_part = f" [{state}]" if state else ""
        detail = f": {desc}" if desc else ""
        lines.append(f"- {ident}{state_part} {title}{detail}")
    return "\n".join(lines) + "\n"


_TERMINAL_STATE_TYPES = frozenset({"completed", "canceled"})


def _relation_issue_summary(issue_node: dict[str, Any] | None) -> dict[str, str]:
    if not issue_node:
        return {}
    state = issue_node.get("state") or {}
    return {
        "identifier": issue_node.get("identifier", ""),
        "title": (issue_node.get("title") or "").strip(),
        "state_name": (state.get("name") or "").strip(),
        "state_type": (state.get("type") or "").strip(),
    }


def extract_issue_relations(issue: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Parse Linear issue relations (blocks / blocked by / related / duplicate)."""
    blockers: list[dict[str, str]] = []
    blocking: list[dict[str, str]] = []
    related: list[dict[str, str]] = []
    duplicate: list[dict[str, str]] = []

    for rel in issue.get("inverseRelations", {}).get("nodes", []) or []:
        if not rel:
            continue
        rtype = (rel.get("type") or "").lower()
        summary = _relation_issue_summary(rel.get("issue"))
        if not summary.get("identifier"):
            continue
        if rtype == "blocks":
            blockers.append(summary)
        elif rtype == "related":
            related.append(summary)
        elif rtype == "duplicate":
            duplicate.append(summary)

    for rel in issue.get("relations", {}).get("nodes", []) or []:
        if not rel:
            continue
        rtype = (rel.get("type") or "").lower()
        summary = _relation_issue_summary(rel.get("relatedIssue"))
        if not summary.get("identifier"):
            continue
        if rtype == "blocks":
            blocking.append(summary)
        elif rtype == "related":
            related.append(summary)
        elif rtype == "duplicate":
            duplicate.append(summary)

    return {
        "blockers": blockers,
        "blocking": blocking,
        "related": related,
        "duplicate": duplicate,
    }


def unfinished_blockers(issue: dict[str, Any]) -> list[dict[str, str]]:
    """Issues blocking this one that are not completed or canceled."""
    return [
        blocker
        for blocker in extract_issue_relations(issue)["blockers"]
        if (blocker.get("state_type") or "").lower() not in _TERMINAL_STATE_TYPES
    ]


def should_defer_for_blockers(
    issue: dict[str, Any],
    session: AgentSession,
) -> bool:
    """Defer investigation on new sessions when open blockers exist."""
    if not settings.linear_defer_on_blockers:
        return False
    if session.action != SessionAction.created:
        return False
    return bool(unfinished_blockers(issue))


def _format_relation_lines(
    heading: str,
    items: list[dict[str, str]],
) -> list[str]:
    if not items:
        return []
    lines = [heading]
    for item in items:
        status = item.get("state_name") or item.get("state_type") or "Unknown"
        title = item.get("title") or ""
        ident = item.get("identifier") or ""
        detail = f": {title}" if title else ""
        lines.append(f"- {ident} [{status}]{detail}")
    return lines


def format_issue_relations_block(issue: dict[str, Any]) -> str:
    """Format issue relations for prompt injection."""
    rels = extract_issue_relations(issue)
    lines: list[str] = []
    lines.extend(_format_relation_lines("Blocked by:", rels["blockers"]))
    lines.extend(_format_relation_lines("Blocks:", rels["blocking"]))
    lines.extend(_format_relation_lines("Related:", rels["related"]))
    lines.extend(_format_relation_lines("Duplicate of:", rels["duplicate"]))
    if not lines:
        return ""
    return "Issue relations:\n" + "\n".join(lines) + "\n"


def format_blocked_deferral_message(
    identifier: str,
    blockers: list[dict[str, str]],
) -> str:
    """User-facing message when deferring work on a blocked issue."""
    lines = [
        f"I have not started work on **{identifier}** — it is still blocked.",
        "",
        "**Blocked by:**",
    ]
    for blocker in blockers:
        status = blocker.get("state_name") or blocker.get("state_type") or "Unknown"
        ident = blocker.get("identifier") or "?"
        title = blocker.get("title") or ""
        lines.append(f"- **{ident}** [{status}] — {title}")
    lines.extend([
        "",
        "Complete or unblock the issue(s) above first. Mention me again on "
        "this issue when ready, or reply in this thread to override.",
    ])
    return "\n".join(lines)


def summarize_conversation_text(conversation_text: str, limit: int = CONVERSATION_SUMMARY_MAX) -> str:
    """Truncate conversation block for planning prompts."""
    text = (conversation_text or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…(conversation truncated for planning)"


def map_hermes_todo_status_to_linear(status: str) -> str:
    normalized = (status or "").strip().lower().replace("-", "_")
    return {
        "pending": _PLAN_PENDING,
        "in_progress": _PLAN_ACTIVE,
        "completed": _PLAN_DONE,
        "cancelled": "canceled",
        "canceled": "canceled",
    }.get(normalized, _PLAN_PENDING)


def hermes_todos_to_linear_plan_steps(
    todos: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Map Hermes todo items to Linear Agent Plan step dicts."""
    steps: list[dict[str, str]] = []
    for item in todos[:PLAN_MAX_STEPS]:
        content = (item.get("content") or "").strip()
        if not content:
            continue
        if len(content) > PLAN_STEP_MAX_LEN:
            content = content[: PLAN_STEP_MAX_LEN - 1] + "…"
        steps.append({
            "content": content,
            "status": map_hermes_todo_status_to_linear(
                str(item.get("status", "")),
            ),
        })
    return steps


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


def _normalize_comment_body(body: str) -> str:
    return " ".join((body or "").split())


def _comment_sort_key(c: dict[str, Any]) -> tuple[str, str]:
    return (c.get("createdAt") or "", c.get("id") or "")


def dedupe_threaded_comments(
    flat: list[tuple[int, dict[str, Any]]],
) -> list[tuple[int, dict[str, Any]]]:
    """Drop threaded children that repeat the parent comment body."""
    deduped: list[tuple[int, dict[str, Any]]] = []
    for i, (depth, c) in enumerate(flat):
        if depth > 0:
            parent_body = ""
            for j in range(i - 1, -1, -1):
                if flat[j][0] == depth - 1:
                    parent_body = flat[j][1].get("body", "") or ""
                    break
            if _normalize_comment_body(parent_body) == _normalize_comment_body(
                c.get("body", "") or "",
            ):
                continue
        deduped.append((depth, c))
    return deduped


def encode_conversation_watermark(flat: list[tuple[int, dict[str, Any]]]) -> str:
    """Encode the latest comment position for delta injection on follow-ups."""
    if not flat:
        return ""
    _, latest = max(flat, key=lambda item: _comment_sort_key(item[1]))
    created_at, comment_id = _comment_sort_key(latest)
    return f"{created_at}\x00{comment_id}"


def _decode_watermark(watermark: str) -> tuple[str, str]:
    if not watermark:
        return ("", "")
    if "\x00" in watermark:
        created_at, comment_id = watermark.split("\x00", 1)
        return (created_at, comment_id)
    return (watermark, "")


def filter_comments_since_watermark(
    flat: list[tuple[int, dict[str, Any]]],
    watermark: str | None,
) -> list[tuple[int, dict[str, Any]]]:
    if not watermark:
        return flat
    since = _decode_watermark(watermark)
    return [
        item for item in flat
        if _comment_sort_key(item[1]) > since
    ]


def build_conversation_text(
    flat: list[tuple[int, dict[str, Any]]],
    *,
    since_watermark: str | None = None,
) -> str:
    """Format issue comments for prompt injection (full or delta since watermark)."""
    if not flat:
        return ""
    sorted_flat = sorted(flat, key=lambda item: _comment_sort_key(item[1]))
    if since_watermark:
        comments = filter_comments_since_watermark(sorted_flat, since_watermark)
        if not comments:
            return ""
        header = "New comments since your last turn:"
    else:
        comments = sorted_flat
        header = "Full conversation (all comments, chronological):"
    lines = [header]
    for depth, c in comments:
        lines.append(_format_comment_line(depth, c))
    return "\n\n" + "\n".join(lines) + "\n"


def _is_agent_author(name: str, agent_bot_name: str) -> bool:
    lowered = (name or "").strip().lower()
    if not lowered:
        return False
    bot = agent_bot_name.strip().lower()
    return lowered == bot or lowered == "hermes"


def resolve_user_request(
    session: AgentSession,
    issue: dict[str, Any],
    *,
    description: str,
    agent_bot_name: str,
    flat_comments: list[tuple[int, dict[str, Any]]],
    since_watermark: str | None = None,
) -> str:
    """Resolve the user message for this turn.

    Prompted turns must never fall back to the issue description (gate templates
    are not the user's new message). When the webhook body is empty, use the
    latest human comment — preferring comments newer than ``since_watermark``.
    """
    identifier = issue.get("identifier", session.issue_identifier)
    body = (session.body or "").strip()
    if body and not _is_linear_system_comment(body):
        return body

    sorted_flat = sorted(flat_comments, key=lambda item: _comment_sort_key(item[1]))

    def _latest_human_comment(
        candidates: list[tuple[int, dict[str, Any]]],
    ) -> str:
        for _, c in reversed(candidates):
            comment_body = (c.get("body") or "").strip()
            if not comment_body or _is_linear_system_comment(comment_body):
                continue
            author = (c.get("user") or {}).get("name", "")
            if _is_agent_author(author, agent_bot_name):
                continue
            return comment_body
        return ""

    if session.action == SessionAction.prompted:
        if since_watermark:
            delta = filter_comments_since_watermark(sorted_flat, since_watermark)
            latest = _latest_human_comment(delta)
            if latest:
                return latest
        latest = _latest_human_comment(sorted_flat)
        if latest:
            return latest
        return f"Follow up on {identifier}"

    if description.strip():
        return description.strip()
    return f"Respond to issue {identifier}"


def is_human_gate_issue(
    issue: dict[str, Any],
    session: AgentSession,
) -> bool:
    """Detect Linear human-gate issues (decision required, not implementation)."""
    combined = "\n".join([
        issue.get("title") or "",
        issue.get("description") or "",
        session.title or "",
        session.description or "",
    ])
    lowered = combined.lower()
    if "🚧" in combined and "human required" in lowered:
        return True
    if "gate — human required" in lowered or "gate - human required" in lowered:
        return True
    return False


class ConversationWatermarkStore:
    """Persist per-session comment watermarks across agent restarts."""

    def __init__(self, path: Path = WATERMARK_STORE_PATH) -> None:
        self._path = path
        self._cache: dict[str, str] = {}
        self._load()

    def get(self, session_id: str) -> str:
        return self._cache.get(session_id, "")

    def set(self, session_id: str, watermark: str) -> None:
        if not session_id or not watermark:
            return
        self._cache[session_id] = watermark
        self._save()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._cache = {
                        str(k): str(v) for k, v in data.items() if v
                    }
        except Exception:
            log.debug("Could not load conversation watermarks", exc_info=True)
            self._cache = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._cache, indent=2),
                encoding="utf-8",
            )
        except Exception:
            log.warning("Could not save conversation watermarks", exc_info=True)


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
    tool_progress: list[str] = field(default_factory=list)
    """Tool-progress lines emitted to the timeline this session (for phase-2 summary)."""

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


def should_finalize_response(
    session: AgentSession,
    tracker: DiscoveryTracker | None,
) -> bool:
    """Phase-2 rewrite is for first-pass investigation; skip on follow-ups."""
    if session.action == SessionAction.prompted:
        return False
    return bool(tracker and tracker.tool_progress)


@dataclass
class SessionPlanTracker:
    """Linear Agent Plans checklist — steps from Hermes, synced to Linear."""

    session_id: str
    linear: LinearClient
    steps: list[dict[str, str]] = field(default_factory=list)

    async def start_fallback(self, identifier: str) -> None:
        """Generic plan when Hermes planning call fails."""
        self.steps = [
            {"content": "Review project context", "status": _PLAN_ACTIVE},
            {"content": "Read issue thread", "status": _PLAN_PENDING},
            {"content": "Confirm target host", "status": _PLAN_PENDING},
            {"content": "Investigate with tools", "status": _PLAN_PENDING},
        ]
        await self._sync()

    async def set_from_hermes(self, step_texts: list[str]) -> None:
        """Apply Hermes-authored plan steps; first step is in progress."""
        texts = [
            _shorten_plan_step(t)
            for t in step_texts
            if t.strip()
        ]
        texts = [t for t in texts if t][:PLAN_MAX_STEPS]
        if len(texts) < 2:
            return
        self.steps = [
            {"content": text, "status": _PLAN_PENDING} for text in texts
        ]
        self.steps[0]["status"] = _PLAN_ACTIVE
        await self._sync()

    async def advance(self) -> None:
        """Complete the active step and start the next (on each tool progress)."""
        for i, step in enumerate(self.steps):
            if step["status"] != _PLAN_ACTIVE:
                continue
            step["status"] = _PLAN_DONE
            if i + 1 < len(self.steps):
                self.steps[i + 1]["status"] = _PLAN_ACTIVE
            await self._sync()
            return

    async def finish(self) -> None:
        self.steps = [
            {**step, "status": _PLAN_DONE} for step in self.steps
        ]
        await self._sync()

    async def _sync(self) -> None:
        if not self.steps:
            return
        try:
            await self.linear.update_plan(self.session_id, self.steps)
        except Exception:
            log.warning("Session plan update failed", exc_info=True)

    async def sync_from_hermes_todos(self, todos: list[dict[str, Any]]) -> None:
        """Project Hermes native todos onto the Linear plan checklist."""
        self.steps = hermes_todos_to_linear_plan_steps(todos)
        if self.steps:
            await self._sync()


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


def _humanize_plan_step(text: str) -> str:
    """Turn command-heavy plan text into a short intent label."""
    t = text.strip()
    t = t.replace("\\~", "~").replace("\\'", "'").replace('\\"', '"')
    t = re.sub(r"\\([~`'_\"])", r"\1", t)

    for pattern, label in _PLAN_COMMAND_LABELS:
        if pattern.search(t):
            return label

    lower = t.lower()
    if re.search(r"navigate|cd to|change directory|open (?:the )?(?:repo|project)", lower):
        if "git" in lower:
            return "Check repository"
        return "Open project"

    if re.search(r"\b(?:run|execute)\b", lower) and re.search(
        r"['\"`]|git |npm |pytest|curl |grep ", lower,
    ):
        for pattern, label in _PLAN_COMMAND_LABELS:
            if pattern.search(t):
                return label
        return "Run investigation command"

    # Drop quoted shell snippets and filesystem paths.
    t = re.sub(r"['\"`][^'\"`]{8,}['\"`]", "", t)
    t = re.sub(r"(?:~|/)[\w./-]+/?", "", t)
    t = re.sub(
        r"\s+(?:with|using|via)\s+(?:the\s+)?(?:command\s+)?['\"`].*?['\"`]",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"\b(?:and\s+)?(?:verify|confirm|check)\s+(?:it(?:'s| is)\s+a\s+)?",
        "",
        t,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", t).strip(" ,;.-")


def _shorten_plan_step(
    text: str,
    max_len: int = PLAN_STEP_MAX_LEN,
    max_words: int = PLAN_STEP_MAX_WORDS,
) -> str:
    """Compress a plan step to a short checklist label."""
    t = _humanize_plan_step(_normalize_progress_markdown(text.strip()))
    t = re.sub(
        r"^(?:i will |i'll |we need to |need to |first,? |then,? |next,? )+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"^(?:read the |check the |review the |search for the )", "", t, flags=re.IGNORECASE)
    t = re.sub(
        r"\b(?:carefully|thoroughly|completely|in order to|so that|in order)\b",
        "",
        t,
        flags=re.IGNORECASE,
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
    return (cut or t[: max_len - 1]) + "…"


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


_GITHUB_PR_URL_RE = re.compile(
    r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)",
    re.I,
)


def extract_github_pr_urls(*texts: str) -> list[str]:
    """Return unique GitHub PR URLs found in any of the given text blobs."""
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


def _pr_external_url_label(url: str) -> str:
    match = _GITHUB_PR_URL_RE.search(url)
    if match:
        return f"PR #{match.group(1)}"
    return "Pull Request"


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


# ── Task Processor ──────────────────────────────────────────────────────


class TaskProcessor:
    """Processes an agent session — analyzes the issue, takes action."""

    def __init__(self, linear: LinearClient) -> None:
        self.linear = linear
        self._viewer_id: str | None = None

    async def ensure_viewer_id(self) -> str:
        if self._viewer_id is None:
            self._viewer_id = await self.linear.get_viewer_id()
        return self._viewer_id

    async def _link_prs_to_session(
        self,
        session_id: str,
        response_text: str,
        *,
        draft_text: str = "",
        tool_progress: list[str] | None = None,
    ) -> list[str]:
        """Attach GitHub PR URLs to the agent session for Linear Diffs."""
        texts = [response_text, draft_text]
        if tool_progress:
            texts.extend(tool_progress)
        pr_urls = extract_github_pr_urls(*texts)
        if not pr_urls:
            return []

        added = [
            {"label": _pr_external_url_label(url), "url": url}
            for url in pr_urls
        ]
        ok = await self.linear.update_session(
            session_id,
            added_external_urls=added,
        )
        if ok:
            log.info(
                "Linked %d PR(s) to session %s: %s",
                len(pr_urls), session_id[:8], ", ".join(pr_urls),
            )
        else:
            log.warning(
                "Failed to link PR(s) to session %s", session_id[:8],
            )
        return pr_urls

    def _ensure_pr_urls_in_response(
        self, response_text: str, pr_urls: list[str],
    ) -> str:
        """Append PR link if Hermes created one but omitted it from the reply."""
        missing = [u for u in pr_urls if u not in response_text]
        if not missing:
            return response_text
        heading = "Pull requests" if len(missing) > 1 else "Pull request"
        lines = "\n".join(f"- {url}" for url in missing)
        return f"{response_text.rstrip()}\n\n**{heading}:**\n{lines}\n"

    async def process(
        self,
        session: AgentSession,
        issue: dict[str, Any] | None,
        *,
        conversation_since: str | None = None,
    ) -> str:
        """Main processing pipeline for a session.

        Returns a conversation watermark (latest comment seen) for follow-up
        delta injection.
        """
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
                return ""

            title = issue.get("title", session.title)
            description = issue.get("description", session.description) or ""
            team_id = issue.get("team", {}).get("id", session.team_id)
            state_type = issue.get("state", {}).get("type", "")
            labels = [l["name"] for l in
                      (issue.get("labels", {}).get("nodes", []) or [])]
            log.info("Issue: %s — %s [%s]", session.issue_identifier, title, state_type)

            # Acknowledge session early (Linear 10s requirement).
            await self.linear.update_session(
                session_id,
                external_urls=[
                    {"label": "Issue", "url": issue.get("url", "")},
                ],
            )

            if should_defer_for_blockers(issue, session):
                blockers = unfinished_blockers(issue)
                identifier = issue.get("identifier", session.issue_identifier)
                await self.linear.send_response(
                    session_id,
                    format_blocked_deferral_message(identifier, blockers),
                )
                log.info(
                    "Deferred session %s — %d open blocker(s)",
                    session_id[:8], len(blockers),
                )
                return ""

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

            # 6. Route to Hermes LLM reasoning
            log.info("Routing to _handle_analysis")
            return await self._handle_analysis(
                session,
                issue,
                session_id,
                conversation_since=conversation_since,
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Error processing session %s", session_id)
            await self.linear.send_error(
                session_id,
                f"An error occurred while processing this issue:\n```\n{e}\n```",
            )
            return conversation_since or ""

    def build_llm_prompt(
        self,
        session: AgentSession,
        issue: dict[str, Any],
        conversation_text: str,
        user_request: str,
        internal_text: str = "",
        skills_context: str = "",
        plan_steps: list[str] | None = None,
        project_issues_block: str = "",
        gate_mode: bool = False,
        relations_block: str = "",
    ) -> str:
        """Assemble the full LLM prompt from session + issue context.

        The prompt consists of:
        1. System declaration ("You are Hermes...")
        2. Execution environment (agent host, default cwd)
        3. LINEAR_API_KEY usage (referenced as env var, never interpolated directly)
        4. Issue context: identifier, title, status, project (name, status, url,
           summary, overview), team, labels, description, workspace guidance
        5. Sibling project issues (recent titles/descriptions in same project)
        6. Conversation thread (all comments, chronological, full body)
        7. Prior session activity (tool calls, thoughts — prompted sessions only)
        8. Session plan checklist
        9. User's message (resolved via resolve_user_request — never issue
           description on prompted follow-ups)
        10. Instruction: context-first work style, then act

        See PLY-32 for the full spec.
        """
        identifier = issue.get("identifier", session.issue_identifier)
        title = issue.get("title", session.title)
        description = issue.get("description", session.description) or ""
        state = issue.get("state", {})
        labels = [l["name"] for l in
                  (issue.get("labels", {}).get("nodes", []) or [])]
        project = issue.get("project") or {}
        project_block = format_project_context_block(
            project,
            fallback_name=session.project_name,
            fallback_summary=session.project_summary,
        )
        guidance_block = format_guidance_block(session.guidance)
        execution_block = format_execution_environment_block()
        team = issue.get("team") or {}
        team_name = team.get("name", "")
        team_key = team.get("key", "")

        skills_block = ""
        if skills_context:
            skills_block = (
                f"\nAvailable Hermes skills (use when relevant — follow fully):\n"
                f"{skills_context}\n"
            )

        plan_block = ""
        if plan_steps:
            plan_block = (
                "\nYour plan for this issue (follow in order — complete context "
                "review steps before shell or system changes):\n"
                + "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan_steps))
                + "\n"
            )

        context_sections = (
            f"{execution_block}\n"
            f"{project_block}"
            f"{project_issues_block}"
            f"{relations_block}"
            f"{guidance_block}"
            f"{conversation_text}\n"
            f"{internal_text}\n"
        )
        gate_block = f"\n{GATE_ISSUE_HINT}\n" if gate_mode else ""

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
                f"Team: {team_name}"
                f"{f' ({team_key})' if team_key else ''}\n"
                f"Labels: {', '.join(labels) or 'none'}\n"
                f"\n"
                f"Read all context below before acting on the user message.\n"
                f"{context_sections}"
                f"{skills_block}"
                f"{plan_block}"
                f"User: {user_request}\n"
                f"\n"
                f"Investigate with your tools, then produce a thorough internal"
                f" draft. Tool actions appear on the Linear timeline as they"
                f" run; a separate pass will write the user-facing reply.\n"
                f"{gate_block}"
                f"\n"
                f"{HERMES_WORK_STYLE}\n"
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
                f"Team: {team_name}"
                f"{f' ({team_key})' if team_key else ''}\n"
                f"Labels: {', '.join(labels) or 'none'}\n"
                f"Description: {description or '(no description)'}\n"
                f"\n"
                f"Read all context below before acting on the user message.\n"
                f"{context_sections}"
                f"{skills_block}"
                f"{plan_block}"
                f"User: {user_request}\n"
                f"\n"
                f"Investigate with your tools, then produce a thorough internal"
                f" draft. Tool actions appear on the Linear timeline as they"
                f" run; a separate pass will write the user-facing reply.\n"
                f"{gate_block}"
                f"\n"
                f"{HERMES_WORK_STYLE}\n"
                f"\n"
                f"Do what needs to be done. Use your tools and report what you"
                f" actually did and found. If it's casual or needs discussion,"
                f" just reply naturally. Do not ask for confirmation before"
                f" starting. Be concise. Do not introduce yourself or list"
                f" capabilities."
            )

    def build_native_turn_message(
        self,
        session: AgentSession,
        issue: dict[str, Any],
        user_request: str,
        *,
        conversation_text: str = "",
        project_issues_block: str = "",
        relations_block: str = "",
        include_full_context: bool = True,
        gate_mode: bool = False,
    ) -> str:
        """Minimal turn payload for Hermes-native mode (one session per assignment)."""
        identifier = issue.get("identifier", session.issue_identifier)
        title = issue.get("title", session.title)
        state = issue.get("state", {})
        labels = [l["name"] for l in
                  (issue.get("labels", {}).get("nodes", []) or [])]
        team = issue.get("team") or {}
        team_name = team.get("name", "")
        team_key = team.get("key", "")

        if include_full_context:
            description = issue.get("description", session.description) or ""
            project_block = format_project_context_block(
                issue.get("project") or {},
                fallback_name=session.project_name,
                fallback_summary=session.project_summary,
            )
            guidance_block = format_guidance_block(session.guidance)
            parts = [
                f"Linear assignment: {identifier} — {title}",
                f"Status: {state.get('name', 'Unknown')}",
                f"Team: {team_name}{f' ({team_key})' if team_key else ''}",
                f"Labels: {', '.join(labels) or 'none'}",
            ]
            if project_block:
                parts.append(project_block.rstrip())
            if project_issues_block:
                parts.append(project_issues_block.rstrip())
            if relations_block:
                parts.append(relations_block.rstrip())
            if guidance_block:
                parts.append(guidance_block.rstrip())
            if description:
                parts.append(f"Issue description:\n{description}")
            if conversation_text.strip():
                parts.append(conversation_text.strip())
            tail = [
                f"User request:\n{user_request}",
            ]
            if gate_mode:
                tail.append(GATE_ISSUE_HINT)
            else:
                tail.extend([
                    HERMES_NATIVE_TODO_HINT,
                    (
                        "Confirm target hosts (VPS, staging, prod) from project "
                        "overview, comments, or sibling issues before SSH, PAM, "
                        "firewall, or package changes."
                    ),
                ])
            tail.extend([
                (
                    "LINEAR_API_KEY is available as $LINEAR_API_KEY for "
                    "GraphQL calls to api.linear.app if needed."
                ),
                LINEAR_OUTPUT_RULES,
                (
                    "Investigate with your tools. Tool progress appears on "
                    "the Linear timeline"
                    + (
                        "; reply with recommendations for Abraham to decide."
                        if gate_mode
                        else "; a follow-up pass may rewrite your final reply "
                        "for the user."
                    )
                ),
            ])
            parts.extend(tail)
            return "\n\n".join(parts)

        # Follow-up turn — delta only; Hermes session retains prior work.
        parts = [
            f"Follow-up on {identifier} — {title}",
            f"Status: {state.get('name', 'Unknown')}",
            f"User message:\n{user_request}",
        ]
        if conversation_text.strip():
            parts.append(conversation_text.strip())
        if gate_mode:
            parts.append(GATE_ISSUE_HINT)
        if relations_block:
            parts.append(relations_block.rstrip())
        if unfinished_blockers(issue):
            parts.append(
                "Note: this issue still has unfinished blockers. "
                "Confirm with the user before substantial implementation work."
            )
        parts.append(LINEAR_OUTPUT_RULES)
        return "\n\n".join(parts)

    async def _sync_plan_from_hermes_todos(
        self,
        plan: SessionPlanTracker,
        hermes_session_id: str,
    ) -> None:
        """Refresh Linear plan checklist from Hermes native todos."""
        todos = await self.linear.fetch_hermes_todos(hermes_session_id)
        await plan.sync_from_hermes_todos(todos)
        if todos:
            log.info(
                "Synced %d Hermes todo(s) to Linear plan for session %s",
                len(todos), hermes_session_id[:8],
            )

    async def _fetch_hermes_skills_context(self) -> str:
        """List Hermes skills from the API server for prompt injection."""
        if not settings.hermes_api_key:
            return ""
        url = f"{settings.hermes_api_url.rstrip('/')}/skills"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {settings.hermes_api_key}"},
                )
                if resp.status_code != 200:
                    return ""
                data = resp.json()
                if not isinstance(data, list):
                    return ""
                lines: list[str] = []
                for skill in data[:30]:
                    if not isinstance(skill, dict):
                        continue
                    name = skill.get("name", "")
                    desc = (skill.get("description") or "")[:120]
                    if name:
                        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
                return "\n".join(lines)
        except Exception:
            log.debug("Could not fetch Hermes skills list", exc_info=True)
            return ""

    def _build_plan_prompt(
        self,
        identifier: str,
        title: str,
        user_request: str,
        description: str,
        skills_context: str = "",
        project_block: str = "",
        conversation_summary: str = "",
    ) -> str:
        skills_block = ""
        if skills_context:
            skills_block = f"\nAvailable skills:\n{skills_context}\n"
        desc = (description or "")[:800]
        project_section = f"\n{project_block}" if project_block.strip() else ""
        conversation_section = ""
        if conversation_summary.strip():
            conversation_section = (
                f"\nIssue conversation (excerpt):\n{conversation_summary}\n"
            )
        return (
            "You are Hermes, planning how to handle a Linear issue.\n"
            "Do not use tools. Output a JSON plan only.\n"
            "\n"
            f"Issue: {identifier} — {title}\n"
            f"Description: {desc or '(none)'}\n"
            f"{project_section}"
            f"{conversation_section}"
            f"{skills_block}"
            f"User request:\n{user_request}\n"
            "\n"
            "Return 3–5 terse checklist steps for this issue.\n"
            "The FIRST steps must establish context before any system or shell "
            "work — e.g. 'Review project context', 'Read issue thread', "
            "'Confirm target host'.\n"
            "Each step: max 6 words, imperative, intent only — not how.\n"
            "Labels like 'Review project context', 'Confirm target host',"
            " 'Check git status', 'Read linear_agent.py'.\n"
            "Never include shell commands, flags, paths, tildes, or quoted strings.\n"
            "Bad: \"Navigate to ~/repo and run 'git status'\".\n"
            "Good: \"Confirm target host\".\n"
            "No explanations, sub-clauses, or 'in order to' phrasing.\n"
            "Do not include 'write final answer' — that is implicit.\n"
            "\n"
            'Respond with JSON only: {"steps": ["...", "..."]}'
        )

    async def _call_llm_plan(
        self,
        identifier: str,
        title: str,
        user_request: str,
        description: str,
        session_id: str = "",
        skills_context: str = "",
        project_block: str = "",
        conversation_summary: str = "",
    ) -> list[str]:
        """Ask Hermes for issue-specific plan steps before investigation."""
        if not settings.hermes_api_key:
            return []

        prompt = self._build_plan_prompt(
            identifier,
            title,
            user_request,
            description,
            skills_context,
            project_block=project_block,
            conversation_summary=conversation_summary,
        )
        headers = {
            "Authorization": f"Bearer {settings.hermes_api_key}",
            "Content-Type": "application/json",
        }
        if session_id:
            headers["X-Hermes-Session-Id"] = f"{session_id}:plan"

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
                    log.info("Hermes plan: %d steps for %s", len(steps), identifier)
                    return steps
                log.warning("Plan call: unparseable or too few steps")
                return []
        except Exception:
            log.warning("Plan call failed", exc_info=True)
            return []

    async def _handle_analysis(
        self,
        session: AgentSession,
        issue: dict[str, Any],
        session_id: str,
        *,
        conversation_since: str | None = None,
    ) -> str:
        """Non-coding task: use LLM to reason and respond intelligently."""
        identifier = issue.get("identifier", session.issue_identifier)
        description = issue.get("description", session.description) or ""
        title = issue.get("title", session.title) or ""

        raw_nodes = issue.get("comments", {}).get("nodes", []) or []
        flat = dedupe_threaded_comments(_flatten_comments(raw_nodes))
        watermark_out = encode_conversation_watermark(flat)

        use_delta = (
            session.action == SessionAction.prompted
            and bool(conversation_since)
        )
        conversation_text = build_conversation_text(
            flat,
            since_watermark=conversation_since if use_delta else None,
        )

        # For prompted sessions, ALSO include session activities (tool calls,
        # thoughts, internal reasoning) as supplementary context above the comments.
        internal_text = ""
        if session.action == SessionAction.prompted and not settings.hermes_native_mode:
            activities = await self.linear.get_session_activities(session_id)
            if activities:
                internal_text = _format_activities_conversation(activities)
                if internal_text:
                    internal_text = "\n\nPrior session activity (Hermes internal):\n" + internal_text

        user_request = resolve_user_request(
            session,
            issue,
            description=description,
            agent_bot_name=settings.linear_agent_bot_name,
            flat_comments=flat,
            since_watermark=conversation_since if use_delta else None,
        )
        relations_block = format_issue_relations_block(issue)
        gate_mode = is_human_gate_issue(issue, session)

        plan = SessionPlanTracker(session_id=session_id, linear=self.linear)

        if settings.hermes_native_mode:
            project_issues_block = ""
            project_id = (issue.get("project") or {}).get("id")
            if project_id:
                siblings = await self.linear.get_project_issue_summaries(
                    project_id, exclude_issue_id=issue.get("id", ""),
                )
                project_issues_block = format_project_issues_block(siblings)

            prompt = self.build_native_turn_message(
                session,
                issue,
                user_request,
                conversation_text=conversation_text,
                project_issues_block=project_issues_block,
                relations_block=relations_block,
                include_full_context=(session.action != SessionAction.prompted),
                gate_mode=gate_mode,
            )
            log.info(
                "Hermes native mode: session=%s action=%s gate=%s",
                session_id[:8], session.action.value, gate_mode,
            )
        else:
            project = issue.get("project") or {}
            project_block = format_project_context_block(
                project,
                fallback_name=session.project_name,
                fallback_summary=session.project_summary,
            )

            project_issues_block = ""
            project_id = project.get("id")
            if project_id:
                siblings = await self.linear.get_project_issue_summaries(
                    project_id, exclude_issue_id=issue.get("id", ""),
                )
                project_issues_block = format_project_issues_block(siblings)

            skills_context = await self._fetch_hermes_skills_context()

            hermes_steps = await self._call_llm_plan(
                identifier=identifier,
                title=title,
                user_request=user_request,
                description=description,
                session_id=session_id,
                skills_context=skills_context,
                project_block=project_block,
                conversation_summary=summarize_conversation_text(conversation_text),
            )
            if hermes_steps:
                await plan.set_from_hermes(hermes_steps)
            else:
                await plan.start_fallback(identifier)

            prompt = self.build_llm_prompt(
                session, issue, conversation_text, user_request, internal_text,
                skills_context=skills_context,
                plan_steps=hermes_steps or [s["content"] for s in plan.steps],
                project_issues_block=project_issues_block,
                gate_mode=gate_mode,
                relations_block=relations_block,
            )

        # Initialize DiscoveryTracker for result-oriented progress updates
        tracker = DiscoveryTracker(session_id=session_id, linear=self.linear)
        await tracker.in_progress(f"Reviewing context for {identifier}")

        # Start background keepalive to continue sending activity during long LLM calls
        keepalive_task = asyncio.create_task(
            self._keep_session_alive(session_id, tracker)
        )

        try:
            draft_text = await self._call_llm(
                prompt, session_id, tracker, plan=plan,
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
            if should_finalize_response(session, tracker):
                finalized = await self._call_llm_finalize(
                    draft=draft_text,
                    user_request=user_request,
                    tool_progress=tracker.tool_progress,
                    session_id=session_id,
                )
                if finalized:
                    response_text = finalized
                    log.info(
                        "Phase-2 summary: draft_len=%d final_len=%d tool_steps=%d",
                        len(draft_text), len(response_text), len(tracker.tool_progress),
                    )
                else:
                    log.warning(
                        "Phase-2 summary failed; sending phase-1 draft",
                    )

            tool_progress = tracker.tool_progress if tracker else None
            pr_urls = await self._link_prs_to_session(
                session_id,
                response_text,
                draft_text=draft_text,
                tool_progress=tool_progress,
            )
            response_text = self._ensure_pr_urls_in_response(
                response_text, pr_urls,
            )

            if settings.hermes_native_mode:
                await self._sync_plan_from_hermes_todos(plan, session_id)
            await plan.finish()
            await self.linear.send_response(session_id, response_text)
            # Task complete — move to "In Review" for the user (not gate issues)
            if (
                session.action != SessionAction.prompted
                and not gate_mode
            ):
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

        return watermark_out

    async def _call_llm(
        self, prompt: str, session_id: str = "",
        tracker: DiscoveryTracker | None = None,
        plan: SessionPlanTracker | None = None,
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
                                    tracker.tool_progress.append(discovery)
                                    await progress_worker.put(discovery)
                                    if plan and not settings.hermes_native_mode:
                                        await plan.advance()
                                    if (
                                        plan
                                        and settings.hermes_native_mode
                                        and progress_data.get("tool") == "todo"
                                        and (progress_data.get("status") or "").lower()
                                        in ("completed", "running")
                                    ):
                                        await self._sync_plan_from_hermes_todos(
                                            plan, session_id,
                                        )
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

    def _build_finalize_prompt(
        self,
        draft: str,
        user_request: str,
        tool_progress: list[str],
    ) -> str:
        """Phase-2 prompt: rewrite investigation draft as conclusions-only reply."""
        timeline = "\n".join(f"- {line}" for line in tool_progress[:25])
        return (
            "You are Hermes, writing the final reply on a Linear issue.\n"
            "\n"
            "The investigation is complete. Tool actions below were already"
            " shown live on the issue timeline. The user read them while you"
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
        session_id: str = "",
    ) -> str | None:
        """Phase 2: non-streaming rewrite — conclusions only, no tool re-run."""
        if not settings.hermes_api_key or not draft.strip():
            return None

        prompt = self._build_finalize_prompt(draft, user_request, tool_progress)
        headers = {
            "Authorization": f"Bearer {settings.hermes_api_key}",
            "Content-Type": "application/json",
        }
        if session_id:
            if settings.hermes_native_mode:
                headers["X-Hermes-Session-Id"] = session_id
            else:
                headers["X-Hermes-Session-Id"] = f"{session_id}:finalize"

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
                    log.warning(
                        "Phase-2 summary: Hermes API returned %d",
                        resp.status_code,
                    )
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
        self._watermark_store = ConversationWatermarkStore()
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
            user_body = activity_body or comment_body or ""
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
            project_name=parsed_context.get("project_name", ""),
            project_summary=parsed_context.get("project_summary", ""),
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
                conversation_since = self._watermark_store.get(
                    session.session_id,
                )
                watermark = await self.processor.process(
                    session,
                    issue,
                    conversation_since=conversation_since or None,
                )
                if watermark:
                    self._watermark_store.set(session.session_id, watermark)
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
            conversation_since = self._watermark_store.get(session.session_id)
            watermark = await self.processor.process(
                session,
                issue,
                conversation_since=conversation_since or None,
            )
            if watermark:
                self._watermark_store.set(session.session_id, watermark)

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

        # Skip terminal states — don't re-process completed/canceled issues
        state_type = payload.get("state", {}).get("type", "") or payload.get("issue", {}).get("state", {}).get("type", "")
        if state_type in ("completed", "canceled"):
            return f"skipped ({state_type} issue)"

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
        "  Model: %s\n"
        "  Workdir: %s\n"
        "═══════════════════════════════════════════════",
        PORT, settings.hermes_model, settings.agent_workdir,
    )

    # Create shared clients
    linear = LinearClient(settings.linear_api_key)
    processor = TaskProcessor(linear)
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
        "model": settings.hermes_model,
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
