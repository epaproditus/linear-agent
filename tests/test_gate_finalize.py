"""Gate-issue profile, skip finalize on prompted, watermark persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("LINEAR_API_KEY", "test-key")
os.environ.setdefault("LINEAR_WEBHOOK_SECRET", "test-secret")

from linear_agent import (
    GATE_ISSUE_HINT,
    AgentSession,
    ConversationWatermarkStore,
    SessionAction,
    TaskProcessor,
    is_human_gate_issue,
    should_finalize_response,
)


GATE_DESCRIPTION = (
    "**🚧 Gate — Human Required**\n\n"
    "Abraham: decide the watcher behavior parameters."
)


@pytest.fixture
def gate_issue() -> dict:
    return {
        "identifier": "PLY-112",
        "title": "GATE D — Define Watcher Parameters",
        "description": GATE_DESCRIPTION,
    }


@pytest.fixture
def gate_session() -> AgentSession:
    return AgentSession(
        session_id="sess-1",
        issue_id="issue-1",
        issue_identifier="PLY-112",
        action=SessionAction.created,
        prompt_context="",
        description=GATE_DESCRIPTION,
    )


def test_is_human_gate_issue_detects_marker(gate_issue: dict, gate_session: AgentSession) -> None:
    assert is_human_gate_issue(gate_issue, gate_session) is True


def test_is_human_gate_issue_negative() -> None:
    issue = {"title": "Fix bug", "description": "Normal task"}
    session = AgentSession(
        session_id="s",
        issue_id="i",
        issue_identifier="X-1",
        action=SessionAction.created,
        prompt_context="",
    )
    assert is_human_gate_issue(issue, session) is False


def test_should_finalize_skips_prompted() -> None:
    session = AgentSession(
        session_id="s",
        issue_id="i",
        issue_identifier="X-1",
        action=SessionAction.prompted,
        prompt_context="",
    )
    tracker = MagicMock()
    tracker.tool_progress = ["Ran shell command"]

    assert should_finalize_response(session, tracker) is False


def test_should_finalize_on_created_with_tools() -> None:
    session = AgentSession(
        session_id="s",
        issue_id="i",
        issue_identifier="X-1",
        action=SessionAction.created,
        prompt_context="",
    )
    tracker = MagicMock()
    tracker.tool_progress = ["Ran shell command"]

    assert should_finalize_response(session, tracker) is True


def test_should_finalize_false_without_tools() -> None:
    session = AgentSession(
        session_id="s",
        issue_id="i",
        issue_identifier="X-1",
        action=SessionAction.created,
        prompt_context="",
    )
    tracker = MagicMock()
    tracker.tool_progress = []

    assert should_finalize_response(session, tracker) is False


def test_conversation_watermark_store_roundtrip(tmp_path: Path) -> None:
    store = ConversationWatermarkStore(path=tmp_path / "watermarks.json")
    store.set("sess-abc", "2026-06-30T19:00:00Z\x00c1")

    reloaded = ConversationWatermarkStore(path=tmp_path / "watermarks.json")
    assert reloaded.get("sess-abc") == "2026-06-30T19:00:00Z\x00c1"
    assert reloaded.get("missing") == ""


def test_native_gate_prompt_includes_hint(
    gate_issue: dict,
    gate_session: AgentSession,
) -> None:
    processor = TaskProcessor(linear=object())  # type: ignore[arg-type]
    msg = processor.build_native_turn_message(
        gate_session,
        gate_issue,
        "Help me pick thresholds",
        include_full_context=True,
        gate_mode=True,
    )

    assert GATE_ISSUE_HINT in msg
    assert "delegate_task" in msg
    assert "recommendations for Abraham" in msg


def test_native_gate_prompted_includes_hint(
    gate_issue: dict,
    gate_session: AgentSession,
) -> None:
    gate_session.action = SessionAction.prompted
    processor = TaskProcessor(linear=object())  # type: ignore[arg-type]
    msg = processor.build_native_turn_message(
        gate_session,
        gate_issue,
        "Why TCP?",
        include_full_context=False,
        gate_mode=True,
    )

    assert GATE_ISSUE_HINT in msg
    assert "Why TCP?" in msg
