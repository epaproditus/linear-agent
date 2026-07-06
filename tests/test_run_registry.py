import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("LINEAR_API_KEY", "test-key")
os.environ.setdefault("LINEAR_WEBHOOK_SECRET", "test-secret")

from run_registry import (
    RunEvent,
    RunRegistry,
    RunState,
    tool_progress_to_audit_event,
)
from linear_agent import append_run_footer, linear_trigger_for_session, AgentSession, SessionAction


class TestRunRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = RunRegistry(Path(self.tmp.name) / "runs.db")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_and_get_run(self) -> None:
        run_id = self.registry.create_run(
            trigger="linear.session.created",
            linear_session_id="sess-1",
            issue_id="issue-1",
            issue_identifier="PLY-1",
        )
        record = self.registry.get_run(run_id)
        assert record is not None
        self.assertEqual(record.state, RunState.created)
        self.assertEqual(record.trigger, "linear.session.created")
        self.assertEqual(record.linear_session_id, "sess-1")
        self.assertEqual(record.issue_identifier, "PLY-1")

    def test_lifecycle_transitions(self) -> None:
        run_id = self.registry.create_run(trigger="linear.session.prompted")
        self.registry.transition(run_id, RunState.running)
        self.registry.transition(
            run_id,
            RunState.completed,
            metadata_patch={"tool_events": 3},
        )
        record = self.registry.get_run(run_id)
        assert record is not None
        self.assertEqual(record.state, RunState.completed)
        self.assertIsNotNone(record.started_at)
        self.assertIsNotNone(record.ended_at)
        self.assertEqual(record.metadata["tool_events"], 3)

    def test_tool_audit_events(self) -> None:
        run_id = self.registry.create_run(trigger="linear.session.created")
        event = tool_progress_to_audit_event(
            {
                "tool": "read_file",
                "status": "completed",
                "label": "README.md",
                "toolCallId": "tc-1",
            },
            summary="Read README.md",
        )
        self.registry.append_event(run_id, event)
        record = self.registry.get_run(run_id)
        assert record is not None
        self.assertEqual(len(record.events), 1)
        self.assertEqual(record.events[0].tool_name, "read_file")
        self.assertEqual(record.events[0].summary, "Read README.md")

    def test_list_runs_filters(self) -> None:
        run_a = self.registry.create_run(
            trigger="linear.session.created",
            issue_id="issue-a",
        )
        run_b = self.registry.create_run(
            trigger="linear.session.created",
            issue_id="issue-b",
        )
        self.registry.transition(run_a, RunState.running)
        self.registry.transition(run_a, RunState.completed)
        self.registry.transition(run_b, RunState.running)
        self.registry.transition(run_b, RunState.failed, error="boom")

        completed = self.registry.list_runs(state="completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0].run_id, run_a)

        issue_b = self.registry.list_runs(issue_id="issue-b")
        self.assertEqual(len(issue_b), 1)
        self.assertEqual(issue_b[0].error, "boom")


class TestRunHelpers(unittest.TestCase):
    def test_linear_trigger_for_session(self) -> None:
        session = AgentSession(
            session_id="sess",
            issue_id="issue",
            issue_identifier="PLY-9",
            action=SessionAction.prompted,
            prompt_context="",
        )
        self.assertEqual(
            linear_trigger_for_session(session),
            "linear.session.prompted",
        )

    def test_append_run_footer(self) -> None:
        run_id = "12345678-abcd-efgh-ijkl-1234567890ab"
        text = append_run_footer("Done.", run_id)
        self.assertIn("12345678", text)
        self.assertTrue(text.endswith("_"))

    def test_append_run_footer_idempotent(self) -> None:
        run_id = "12345678-abcd-efgh-ijkl-1234567890ab"
        once = append_run_footer("Done.", run_id)
        twice = append_run_footer(once, run_id)
        self.assertEqual(once, twice)
