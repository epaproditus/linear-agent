"""Linear issue relation (blocks / blocked by) parsing and deferral."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("LINEAR_API_KEY", "test-key")
os.environ.setdefault("LINEAR_WEBHOOK_SECRET", "test-secret")

from linear_agent import (
    AgentSession,
    SessionAction,
    TaskProcessor,
    extract_issue_relations,
    format_blocked_deferral_message,
    format_issue_relations_block,
    should_defer_for_blockers,
    unfinished_blockers,
)


def _issue(
    *,
    blockers: list[dict] | None = None,
    blocking: list[dict] | None = None,
) -> dict:
    inverse_nodes = []
    for blocker in blockers or []:
        inverse_nodes.append({
            "type": "blocks",
            "issue": blocker,
        })
    relation_nodes = []
    for blocked in blocking or []:
        relation_nodes.append({
            "type": "blocks",
            "relatedIssue": blocked,
        })
    return {
        "identifier": "PLY-113",
        "title": "Implement watcher",
        "inverseRelations": {"nodes": inverse_nodes},
        "relations": {"nodes": relation_nodes},
    }


def test_extract_blockers_from_inverse_relations() -> None:
    issue = _issue(blockers=[{
        "identifier": "PLY-112",
        "title": "Define parameters",
        "state": {"name": "In Progress", "type": "started"},
    }])
    rels = extract_issue_relations(issue)
    assert len(rels["blockers"]) == 1
    assert rels["blockers"][0]["identifier"] == "PLY-112"


def test_unfinished_blockers_ignores_completed() -> None:
    issue = _issue(blockers=[
        {
            "identifier": "PLY-111",
            "title": "Done blocker",
            "state": {"name": "Done", "type": "completed"},
        },
        {
            "identifier": "PLY-112",
            "title": "Open blocker",
            "state": {"name": "In Progress", "type": "started"},
        },
    ])
    open_blockers = unfinished_blockers(issue)
    assert len(open_blockers) == 1
    assert open_blockers[0]["identifier"] == "PLY-112"


def test_should_defer_on_created_with_open_blocker() -> None:
    issue = _issue(blockers=[{
        "identifier": "PLY-112",
        "title": "Gate",
        "state": {"name": "Todo", "type": "unstarted"},
    }])
    session = AgentSession(
        session_id="s",
        issue_id="i",
        issue_identifier="PLY-113",
        action=SessionAction.created,
        prompt_context="",
    )
    assert should_defer_for_blockers(issue, session) is True


def test_should_not_defer_on_prompted() -> None:
    issue = _issue(blockers=[{
        "identifier": "PLY-112",
        "title": "Gate",
        "state": {"name": "Todo", "type": "unstarted"},
    }])
    session = AgentSession(
        session_id="s",
        issue_id="i",
        issue_identifier="PLY-113",
        action=SessionAction.prompted,
        prompt_context="",
    )
    assert should_defer_for_blockers(issue, session) is False


def test_defer_disabled_via_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import linear_agent

    monkeypatch.setattr(linear_agent.settings, "linear_defer_on_blockers", False)
    issue = _issue(blockers=[{
        "identifier": "PLY-112",
        "title": "Gate",
        "state": {"name": "Todo", "type": "unstarted"},
    }])
    session = AgentSession(
        session_id="s",
        issue_id="i",
        issue_identifier="PLY-113",
        action=SessionAction.created,
        prompt_context="",
    )
    assert should_defer_for_blockers(issue, session) is False


def test_format_issue_relations_block() -> None:
    issue = _issue(
        blockers=[{
            "identifier": "PLY-112",
            "title": "Define parameters",
            "state": {"name": "In Review", "type": "started"},
        }],
        blocking=[{
            "identifier": "PLY-114",
            "title": "Deploy watcher",
            "state": {"name": "Todo", "type": "unstarted"},
        }],
    )
    block = format_issue_relations_block(issue)
    assert "Blocked by:" in block
    assert "PLY-112 [In Review]" in block
    assert "Blocks:" in block
    assert "PLY-114" in block


def test_format_blocked_deferral_message() -> None:
    msg = format_blocked_deferral_message("PLY-113", [{
        "identifier": "PLY-112",
        "title": "Define parameters",
        "state_name": "In Progress",
        "state_type": "started",
    }])
    assert "PLY-113" in msg
    assert "PLY-112" in msg
    assert "blocked" in msg.lower()


def test_native_prompt_includes_relations() -> None:
    processor = TaskProcessor(linear=object())  # type: ignore[arg-type]
    issue = _issue(blockers=[{
        "identifier": "PLY-112",
        "title": "Define parameters",
        "state": {"name": "In Progress", "type": "started"},
    }])
    issue.update({
        "description": "Build the watcher",
        "state": {"name": "Todo"},
        "team": {"name": "Playground", "key": "PLY"},
        "labels": {"nodes": []},
    })
    session = AgentSession(
        session_id="s",
        issue_id="i",
        issue_identifier="PLY-113",
        action=SessionAction.created,
        prompt_context="",
    )
    relations = format_issue_relations_block(issue)
    msg = processor.build_native_turn_message(
        session,
        issue,
        "Please implement",
        relations_block=relations,
        include_full_context=True,
    )
    assert "Blocked by:" in msg
    assert "PLY-112" in msg
