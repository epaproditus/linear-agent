"""Prompted-turn user request resolution and conversation delta injection."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("LINEAR_API_KEY", "test-key")
os.environ.setdefault("LINEAR_WEBHOOK_SECRET", "test-secret")

from linear_agent import (
    AgentSession,
    SessionAction,
    TaskProcessor,
    build_conversation_text,
    dedupe_threaded_comments,
    encode_conversation_watermark,
    filter_comments_since_watermark,
    resolve_user_request,
)


GATE_DESCRIPTION = (
    "**🚧 Gate — Human Required**\n\n"
    "Abraham: decide the watcher behavior parameters."
)


def _comment(
    *,
    cid: str,
    created_at: str,
    author: str,
    body: str,
    children: list[dict] | None = None,
) -> dict:
    node: dict = {
        "id": cid,
        "createdAt": created_at,
        "body": body,
        "user": {"name": author},
    }
    if children:
        node["children"] = {"nodes": children}
    return node


@pytest.fixture
def issue() -> dict:
    return {
        "identifier": "PLY-112",
        "title": "GATE D — Define Watcher Parameters",
        "description": GATE_DESCRIPTION,
        "comments": {
            "nodes": [
                _comment(
                    cid="c1",
                    created_at="2026-06-30T18:43:16.000Z",
                    author="me@mr-romero.com",
                    body="@hermes check Facebook logs",
                ),
                _comment(
                    cid="c2",
                    created_at="2026-06-30T19:00:00.000Z",
                    author="Hermes",
                    body="Here are the findings.",
                ),
                _comment(
                    cid="c3",
                    created_at="2026-06-30T19:05:00.000Z",
                    author="me@mr-romero.com",
                    body="Why can't we do TCP?",
                ),
            ],
        },
    }


def test_resolve_user_request_prompted_ignores_issue_description(issue: dict) -> None:
    session = AgentSession(
        session_id="sess-1",
        issue_id="issue-1",
        issue_identifier="PLY-112",
        action=SessionAction.prompted,
        prompt_context="",
        body="",
    )
    flat = [(0, c) for c in issue["comments"]["nodes"]]

    request = resolve_user_request(
        session,
        issue,
        description=GATE_DESCRIPTION,
        agent_bot_name="Hermes",
        flat_comments=flat,
    )

    assert request == "Why can't we do TCP?"
    assert GATE_DESCRIPTION not in request


def test_resolve_user_request_prompted_prefers_webhook_body(issue: dict) -> None:
    session = AgentSession(
        session_id="sess-1",
        issue_id="issue-1",
        issue_identifier="PLY-112",
        action=SessionAction.prompted,
        prompt_context="",
        body="Explain WireGuard please",
    )
    flat = [(0, c) for c in issue["comments"]["nodes"]]

    request = resolve_user_request(
        session,
        issue,
        description=GATE_DESCRIPTION,
        agent_bot_name="Hermes",
        flat_comments=flat,
    )

    assert request == "Explain WireGuard please"


def test_resolve_user_request_prompted_uses_delta_comments(issue: dict) -> None:
    session = AgentSession(
        session_id="sess-1",
        issue_id="issue-1",
        issue_identifier="PLY-112",
        action=SessionAction.prompted,
        prompt_context="",
        body="",
    )
    flat = [(0, c) for c in issue["comments"]["nodes"]]
    watermark = encode_conversation_watermark([(0, issue["comments"]["nodes"][1])])

    request = resolve_user_request(
        session,
        issue,
        description=GATE_DESCRIPTION,
        agent_bot_name="Hermes",
        flat_comments=flat,
        since_watermark=watermark,
    )

    assert request == "Why can't we do TCP?"


def test_build_conversation_text_delta_only() -> None:
    flat = [
        (0, _comment(cid="c1", created_at="2026-06-30T18:00:00Z", author="User", body="first")),
        (0, _comment(cid="c2", created_at="2026-06-30T19:00:00Z", author="User", body="second")),
    ]
    watermark = encode_conversation_watermark([flat[0]])

    text = build_conversation_text(flat, since_watermark=watermark)

    assert "New comments since your last turn" in text
    assert "second" in text
    assert "first" not in text


def test_build_conversation_text_full_thread() -> None:
    flat = [
        (0, _comment(cid="c1", created_at="2026-06-30T18:00:00Z", author="User", body="first")),
        (0, _comment(cid="c2", created_at="2026-06-30T19:00:00Z", author="User", body="second")),
    ]

    text = build_conversation_text(flat)

    assert "Full conversation" in text
    assert "first" in text
    assert "second" in text


def test_dedupe_threaded_comments() -> None:
    parent = _comment(
        cid="p",
        created_at="2026-06-30T18:00:00Z",
        author="User",
        body="same text",
        children=[
            _comment(
                cid="c",
                created_at="2026-06-30T18:01:00Z",
                author="User",
                body="same text",
            ),
        ],
    )
    from linear_agent import _flatten_comments

    flat = _flatten_comments([parent])
    deduped = dedupe_threaded_comments(flat)

    assert len(deduped) == 1


def test_native_prompted_message_omits_full_conversation(issue: dict) -> None:
    processor = TaskProcessor(linear=object())  # type: ignore[arg-type]
    session = AgentSession(
        session_id="sess-1",
        issue_id="issue-1",
        issue_identifier="PLY-112",
        action=SessionAction.prompted,
        prompt_context="",
        body="Why can't we do TCP?",
    )
    watermark = encode_conversation_watermark(
        [(0, issue["comments"]["nodes"][1])],
    )
    delta = build_conversation_text(
        [(0, c) for c in issue["comments"]["nodes"]],
        since_watermark=watermark,
    )

    msg = processor.build_native_turn_message(
        session,
        issue,
        "Why can't we do TCP?",
        thread_context=delta,
        include_full_context=False,
    )

    assert "[Replying on Linear issue PLY-112" in msg
    assert GATE_DESCRIPTION not in msg
    assert "Full conversation" not in msg
    assert "Why can't we do TCP?" in msg
