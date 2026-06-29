"""PLY-78: Verify how much Linear project context reaches the LLM prompt."""

from __future__ import annotations

import re

import pytest

from linear_agent import (
    AgentSession,
    SessionAction,
    TaskProcessor,
    parse_prompt_context,
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
    )


def test_parse_prompt_context_does_not_extract_project() -> None:
    parsed = parse_prompt_context(LINEAR_PROMPT_CONTEXT_EXAMPLE)

    assert "project" not in parsed
    assert parsed["identifier"] == "PLY-78"
    assert parsed["title"] == "Determine how much project context is injected"
    assert parsed["guidance"] == ["Always follow coding standards"]

    # Project summary text inside <project> must not appear in any parsed field.
    assert "Hermes on any issue" not in str(parsed.values())


def test_build_llm_prompt_includes_project_name_only(
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
            "description": "## Architecture\n\nLinear webhook -> Hermes API",
            "summary": "Fast autonomous agent for Linear issues",
        },
    }

    prompt = processor.build_llm_prompt(
        sample_session,
        issue,
        conversation_text="",
        user_request="Please investigate",
    )

    assert "Project: Hermes as Linear agent" in prompt
    assert "Linear webhook" not in prompt
    assert "Fast autonomous agent" not in prompt
    assert "Architecture" not in prompt


def test_build_llm_prompt_omits_stored_guidance(
    processor: TaskProcessor, sample_session: AgentSession
) -> None:
    assert sample_session.guidance == ["Always follow coding standards"]

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

    assert "Always follow coding standards" not in prompt
    assert "guidance" not in prompt.lower()


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

    match = re.search(r"project\s*\{([^}]+)\}", GQL_ISSUE_BY_ID)
    assert match is not None
    fields = match.group(1).split()
    assert fields == ["id", "name"]
