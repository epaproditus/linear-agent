"""PLY-153: Slack gateway session learnings encoded in Linear agent prompts."""

from __future__ import annotations

import json
import os
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("LINEAR_API_KEY", "test-key")
os.environ.setdefault("LINEAR_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("HERMES_NATIVE_MODE", "0")

from linear_agent import (  # noqa: E402
    HERMES_WORK_STYLE,
    LINEAR_OUTPUT_RULES,
    TaskProcessor,
)

FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "fixtures"
    / "sample_slack_conversation.json"
)


def _load_fixture() -> dict:
    with FIXTURE.open(encoding="utf-8") as f:
        return json.load(f)


def _analyze_slack_session(data: dict) -> dict[str, int]:
    """Return key metrics that motivated PLY-153 prompt changes."""
    msgs = data["messages"]
    roles = Counter(m["role"] for m in msgs)
    terminal_calls = 0
    single_command_terminals = 0

    for m in msgs:
        if m["role"] != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") != "terminal":
                continue
            terminal_calls += 1
            try:
                args = json.loads(fn.get("arguments", "{}"))
                cmd = (args.get("command") or "").strip()
            except json.JSONDecodeError:
                cmd = ""
            if cmd and cmd.count(";") == 0 and "\n" not in cmd:
                single_command_terminals += 1

    return {
        "user_messages": roles.get("user", 0),
        "assistant_messages": roles.get("assistant", 0),
        "tool_messages": roles.get("tool", 0),
        "terminal_calls": terminal_calls,
        "single_command_terminals": single_command_terminals,
        "message_count": data.get("message_count", len(msgs)),
    }


class TestSlackFixtureMetrics(unittest.TestCase):
  """Regression guard: fixture documents the failure mode we prompt against."""

  @classmethod
  def setUpClass(cls) -> None:
    if not FIXTURE.is_file():
      raise unittest.SkipTest(f"fixture missing: {FIXTURE}")
    cls.metrics = _analyze_slack_session(_load_fixture())

  def test_fixture_is_large_troubleshooting_thread(self) -> None:
    self.assertGreaterEqual(self.metrics["message_count"], 100)
    self.assertGreaterEqual(self.metrics["user_messages"], 10)
    self.assertGreaterEqual(self.metrics["assistant_messages"], 50)

  def test_terminal_sprawl_documented(self) -> None:
    self.assertGreaterEqual(self.metrics["terminal_calls"], 40)
    self.assertGreater(
      self.metrics["single_command_terminals"],
      self.metrics["terminal_calls"] // 5,
    )

  def test_assistant_verbosity_exceeds_user_turns(self) -> None:
    self.assertGreater(
      self.metrics["assistant_messages"],
      self.metrics["user_messages"] * 3,
    )


class TestSlackGatewayPromptRules(unittest.TestCase):
  def test_work_style_disambiguation(self) -> None:
    self.assertIn("Disambiguate before deep diagnosis", HERMES_WORK_STYLE)
    self.assertIn("clarifying question", HERMES_WORK_STYLE.lower())

  def test_work_style_batch_diagnostics(self) -> None:
    self.assertIn("Batch shell diagnostics", HERMES_WORK_STYLE)
    self.assertIn("one terminal script", HERMES_WORK_STYLE.lower())

  def test_work_style_minimal_interim_text(self) -> None:
    self.assertIn("During tool loops keep assistant text minimal", HERMES_WORK_STYLE)
    self.assertIn("Let me check", HERMES_WORK_STYLE)

  def test_output_rules_no_process_narration(self) -> None:
    self.assertIn("No process narration", LINEAR_OUTPUT_RULES)

  def test_finalize_prompt_inherits_reply_style(self) -> None:
    processor = TaskProcessor(linear=MagicMock())
    prompt = processor._build_finalize_prompt(
      draft="I checked systemctl and found the service down.",
      user_request="Why is the GUI broken?",
      tool_progress=["Checked hermes-dashboard.service"],
    )
    self.assertIn("Conclusions, findings", prompt)
    self.assertIn("No process narration", prompt)
