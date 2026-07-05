"""PLY-153: Linear native mode follows Slack gateway per-turn context feeding."""

from __future__ import annotations

import json
import os
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("LINEAR_API_KEY", "test-key")
os.environ.setdefault("LINEAR_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("HERMES_NATIVE_MODE", "1")

from linear_agent import (  # noqa: E402
    THREAD_CONTEXT_FOOTER,
    THREAD_CONTEXT_HEADER,
    AgentSession,
    SessionAction,
    TaskProcessor,
    _normalize_comment_body,
    build_thread_context_block,
    encode_conversation_watermark,
)

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "fixtures"
    / "sample_slack_conversation.json"
)


def _comment(
    *,
    cid: str,
    created_at: str,
    author: str,
    body: str,
) -> dict:
    return {
        "id": cid,
        "createdAt": created_at,
        "body": body,
        "user": {"name": author},
    }


class TestSlackFixtureTurnShape(unittest.TestCase):
    """Document how the Slack gateway structures each user turn."""

    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE.is_file():
            raise unittest.SkipTest(f"fixture missing: {FIXTURE}")
        cls.users = [
            m for m in json.loads(FIXTURE.read_text())["messages"]
            if m["role"] == "user"
        ]

    def test_each_turn_has_thread_context_then_new_message(self) -> None:
        for content in (u["content"] for u in self.users):
            self.assertIn("[Thread context", content)
            self.assertIn("[End of thread context]", content)
            new_msg = content.split("[End of thread context]", 1)[1].strip()
            self.assertTrue(new_msg)

    def test_thread_context_grows_with_prior_human_turns(self) -> None:
        counts: list[int] = []
        for content in (u["content"] for u in self.users):
            block = content.split("[End of thread context]", 1)[0]
            abraham_lines = [
                line for line in block.split("\n")
                if line.startswith("Abraham:")
            ]
            counts.append(len(abraham_lines))
        self.assertGreater(max(counts), min(counts))


class TestBuildThreadContextBlock(unittest.TestCase):
    def test_slack_style_human_only_lines(self) -> None:
        flat = [
            (0, _comment(cid="c1", created_at="2026-07-05T10:00:00Z", author="Abraham", body="first")),
            (0, _comment(cid="c2", created_at="2026-07-05T10:05:00Z", author="Hermes", body="agent reply")),
            (0, _comment(cid="c3", created_at="2026-07-05T10:10:00Z", author="Abraham", body="second")),
        ]
        block = build_thread_context_block(
            flat,
            agent_bot_name="Hermes",
            exclude_bodies=frozenset({_normalize_comment_body("second")}),
        )
        self.assertIn(THREAD_CONTEXT_HEADER, block)
        self.assertIn(THREAD_CONTEXT_FOOTER, block)
        self.assertIn("Abraham: first", block)
        self.assertNotIn("Hermes: agent reply", block)
        self.assertNotIn("Abraham: second", block)

    def test_delta_since_watermark(self) -> None:
        flat = [
            (0, _comment(cid="c1", created_at="2026-07-05T10:00:00Z", author="Abraham", body="old")),
            (0, _comment(cid="c2", created_at="2026-07-05T11:00:00Z", author="Hermes", body="done")),
            (0, _comment(cid="c3", created_at="2026-07-05T12:00:00Z", author="Abraham", body="new")),
        ]
        watermark = encode_conversation_watermark([flat[1]])
        block = build_thread_context_block(
            flat,
            since_watermark=watermark,
            agent_bot_name="Hermes",
            exclude_bodies=frozenset({_normalize_comment_body("new")}),
        )
        self.assertNotIn("Abraham: old", block)
        self.assertNotIn("Abraham: new", block)


class TestNativeTurnMatchesSlackFeeding(unittest.TestCase):
    def setUp(self) -> None:
        self.processor = TaskProcessor(linear=MagicMock())
        self.issue = {
            "identifier": "PLY-153",
            "title": "Slack gateway parity",
            "description": "Issue body for first turn only",
            "state": {"name": "In Progress"},
            "team": {"name": "Platform", "key": "PLY"},
            "labels": {"nodes": []},
            "project": {"name": "Hermes"},
        }

    def test_created_turn_issue_card_plus_thread_plus_message(self) -> None:
        session = AgentSession(
            session_id="s1",
            issue_id="i1",
            issue_identifier="PLY-153",
            action=SessionAction.created,
            prompt_context="",
            title="Slack gateway parity",
            description="Issue body for first turn only",
        )
        thread = build_thread_context_block(
            [(0, _comment(cid="c1", created_at="2026-07-05T10:00:00Z", author="Abraham", body="prior note"))],
            exclude_bodies=frozenset({_normalize_comment_body("do the work")}),
        )
        msg = self.processor.build_native_turn_message(
            session,
            self.issue,
            "do the work",
            thread_context=thread,
            include_full_context=True,
        )
        self.assertIn("Linear assignment: PLY-153", msg)
        self.assertIn("Issue description:", msg)
        self.assertIn(THREAD_CONTEXT_HEADER, msg)
        self.assertIn("Abraham: prior note", msg)
        self.assertIn("do the work", msg)
        self.assertNotIn("User request:", msg)

    def test_prompted_turn_is_thread_context_plus_new_message_only(self) -> None:
        session = AgentSession(
            session_id="s1",
            issue_id="i1",
            issue_identifier="PLY-153",
            action=SessionAction.prompted,
            prompt_context="",
            body="follow up please",
        )
        thread = build_thread_context_block(
            [
                (0, _comment(cid="c1", created_at="2026-07-05T10:00:00Z", author="Abraham", body="earlier")),
            ],
            exclude_bodies=frozenset({_normalize_comment_body("follow up please")}),
        )
        msg = self.processor.build_native_turn_message(
            session,
            self.issue,
            "follow up please",
            thread_context=thread,
            include_full_context=False,
        )
        self.assertRegex(msg, r"\[Replying on Linear issue PLY-153")
        self.assertIn("follow up please", msg)
        self.assertIn("Abraham: earlier", msg)
        self.assertNotIn("Issue description:", msg)
        self.assertNotIn("Linear output rules:", msg.lower())


class TestSlackFixtureMetrics(unittest.TestCase):
    """Regression metrics from the sample Slack session."""

    @classmethod
    def setUpClass(cls) -> None:
        if not FIXTURE.is_file():
            raise unittest.SkipTest(f"fixture missing: {FIXTURE}")
        msgs = json.loads(FIXTURE.read_text())["messages"]
        cls.roles = Counter(m["role"] for m in msgs)

    def test_assistant_verbosity_exceeds_user_turns(self) -> None:
        self.assertGreater(
            self.roles.get("assistant", 0),
            self.roles.get("user", 0) * 3,
        )
