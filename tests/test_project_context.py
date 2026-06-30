"""PLY-78: Verify Linear project and guidance context in LLM prompts."""

from __future__ import annotations

import re

import pytest

from linear_agent import (
    AgentSession,
    PROJECT_CONTEXT_MAX_LEN,
    SessionAction,
    TaskProcessor,
    format_execution_environment_block,
    format_guidance_block,
    format_project_context_block,
    format_project_issues_block,
    parse_prompt_context,
    summarize_conversation_text,
)


LINEAR_PROMPT_CONTEXT_EXAMPLE = """
<issue identifier="PLY-78">
<title>Determine how much project context is injected</title>
<description>Investigate what project data is provided.</description>
<team name="Engineering"/>
<label>research</label>
<project name="Hermes as Linear agent">@-mention @Hermes on any issue — fast agent.</project>
</issue>

<primary-directive-thread comment-id="abc"><comment author="User" created-at="2026-06-28 12:00:00">Please investigate</comment></primary-directive-thread>

<guidance><guidance-rule origin="team" team-name="Engineering">Always follow coding standards</guidance-rule></guidance>
"""


class _StubLinear:
    """Minimal stub — build_llm_prompt does not call Linear."""


@pytest.fixture
def processor() -> TaskProcessor:
    return TaskProcessor(linear=_StubLinear())  # type: ignore[arg-type]


@pytest.fixture
def sample_session() -> AgentSession:
    parsed = parse_prompt_context(LINEAR_PROMPT_CONTEXT_EXAMPLE)
    return AgentSession(
        session_id="sess-1",
        issue_id="issue-1",
        issue_identifier="PLY-78",
        action=SessionAction.created,
        prompt_context=LINEAR_PROMPT_CONTEXT_EXAMPLE,
        body="Please investigate",
        title="Determine how much project context is injected",
        description="Investigate what project data is provided.",
        guidance=parsed["guidance"],
        project_name=parsed.get("project_name", ""),
        project_summary=parsed.get("project_summary", ""),
    )


def test_parse_prompt_context_extracts_project() -> None:
    parsed = parse_prompt_context(LINEAR_PROMPT_CONTEXT_EXAMPLE)

    assert parsed["project_name"] == "Hermes as Linear agent"
    assert parsed["project_summary"] == "@-mention @Hermes on any issue — fast agent."
    assert parsed["guidance"] == ["Always follow coding standards"]


def test_format_project_context_block_from_graphql() -> None:
    block = format_project_context_block({
        "name": "Hermes as Linear agent",
        "description": "Fast autonomous agent for Linear issues",
        "content": "## Architecture\n\nLinear webhook -> Hermes API",
        "url": "https://linear.app/team/project/hermes",
        "status": {"name": "In Progress", "type": "started"},
    })

    assert "Project: Hermes as Linear agent" in block
    assert "Project status: In Progress (started)" in block
    assert "Project URL: https://linear.app/team/project/hermes" in block
    assert "Project summary: Fast autonomous agent" in block
    assert "Linear webhook -> Hermes API" in block


def test_format_project_context_block_uses_prompt_context_fallback() -> None:
    block = format_project_context_block(
        None,
        fallback_name="Hermes as Linear agent",
        fallback_summary="@-mention @Hermes on any issue — fast agent.",
    )

    assert "Project: Hermes as Linear agent" in block
    assert "Project summary: @-mention @Hermes" in block


def test_format_project_context_block_truncates_long_content() -> None:
    long_content = "x" * (PROJECT_CONTEXT_MAX_LEN + 500)
    block = format_project_context_block({
        "name": "Big project",
        "content": long_content,
    })

    assert "…(truncated)" in block
    assert len(block) < len(long_content)


def test_format_guidance_block() -> None:
    block = format_guidance_block(["Always follow coding standards", "Use PRs"])
    assert "Team/workspace guidance:" in block
    assert "- Always follow coding standards" in block
    assert "- Use PRs" in block
    assert format_guidance_block([]) == ""


def test_build_llm_prompt_includes_rich_project_and_guidance(
    processor: TaskProcessor, sample_session: AgentSession
) -> None:
    issue = {
        "identifier": "PLY-78",
        "title": "Determine how much project context is injected",
        "description": "Investigate what project data is provided.",
        "state": {"name": "In Progress", "type": "started"},
        "team": {"name": "Engineering", "key": "ENG"},
        "labels": {"nodes": [{"name": "research"}]},
        "project": {
            "id": "proj-1",
            "name": "Hermes as Linear agent",
            "description": "Fast autonomous agent for Linear issues",
            "content": "## Architecture\n\nLinear webhook -> Hermes API",
            "url": "https://linear.app/team/project/hermes",
            "status": {"name": "In Progress", "type": "started"},
        },
    }

    sibling_block = format_project_issues_block([
        {
            "identifier": "PLY-40",
            "title": "Harden VPS SSH PAM",
            "state": {"name": "Done"},
            "description": "Work on vps.example.com — not local machine.",
        },
    ])

    prompt = processor.build_llm_prompt(
        sample_session,
        issue,
        conversation_text="\n\nFull conversation:\n- User: use the VPS\n",
        user_request="Please investigate",
        project_issues_block=sibling_block,
    )

    assert "Project: Hermes as Linear agent" in prompt
    assert "Linear webhook -> Hermes API" in prompt
    assert "Always follow coding standards" in prompt
    assert "Team/workspace guidance:" in prompt
    assert "Execution environment" in prompt
    assert "Agent host:" in prompt
    assert "Read all context below before acting" in prompt
    assert "PLY-40" in prompt
    assert "vps.example.com" in prompt
    assert "Context before action" in prompt
    # Context must appear before the user request
    assert prompt.index("Full conversation") < prompt.index("User: Please investigate")


def test_build_llm_prompt_omits_raw_prompt_context_xml(
    processor: TaskProcessor, sample_session: AgentSession
) -> None:
    issue = {
        "identifier": "PLY-78",
        "title": "Test",
        "description": "Desc",
        "state": {"name": "Todo"},
        "team": {"name": "Eng", "key": "E"},
        "labels": {"nodes": []},
        "project": {"name": "Hermes as Linear agent"},
    }

    prompt = processor.build_llm_prompt(
        sample_session,
        issue,
        conversation_text="",
        user_request="Go",
    )

    assert "<project" not in prompt
    assert "primary-directive-thread" not in prompt


def test_gql_issue_by_id_project_fields() -> None:
    """Document which project fields the issue fetch query requests."""
    from linear_agent import GQL_ISSUE_BY_ID

    match = re.search(r"project\s*\{([^}]+)\}", GQL_ISSUE_BY_ID, re.DOTALL)
    assert match is not None
    fields = match.group(1)
    for field in ("id", "name", "description", "content", "url", "status"):
        assert field in fields


def test_format_execution_environment_block() -> None:
    block = format_execution_environment_block()
    assert "Execution environment" in block
    assert "Agent host:" in block
    assert "explicitly SSH" in block


def test_format_project_issues_block() -> None:
    block = format_project_issues_block([
        {
            "identifier": "INFRA-12",
            "title": "VPS baseline",
            "state": {"name": "Done"},
            "description": "Target: vps.prod.example.com",
        },
    ])
    assert "INFRA-12" in block
    assert "vps.prod.example.com" in block


def test_summarize_conversation_text_truncates() -> None:
    long_text = "x" * 5000
    summary = summarize_conversation_text(long_text, limit=100)
    assert len(summary) < 200
    assert "truncated" in summary


def test_build_plan_prompt_requires_context_first_steps() -> None:
    processor = TaskProcessor(linear=_StubLinear())  # type: ignore[arg-type]
    prompt = processor._build_plan_prompt(
        "PLY-99",
        "Harden PAM",
        "Lock down SSH PAM on the server",
        "Security hardening task",
        project_block="Project: Infra\n",
        conversation_summary="- User: work on the VPS\n",
    )
    assert "Review project context" in prompt or "Confirm target host" in prompt
    assert "Project: Infra" in prompt
    assert "VPS" in prompt
