import os
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("LINEAR_API_KEY", "test-key")
os.environ.setdefault("LINEAR_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("HERMES_NATIVE_MODE", "0")

from linear_agent import (
    AgentSession,
    HERMES_NATIVE_TODO_HINT,
    LINEAR_OUTPUT_RULES,
    THREAD_CONTEXT_FOOTER,
    THREAD_CONTEXT_HEADER,
    SessionAction,
    SessionPlanTracker,
    Settings,
    TaskProcessor,
    format_project_context_block,
    format_project_issues_block,
    hermes_todos_to_linear_plan_steps,
    map_hermes_todo_status_to_linear,
    parse_prompt_context,
)


class TestHermesTodoMapping(unittest.TestCase):
    def test_status_mapping(self) -> None:
        self.assertEqual(map_hermes_todo_status_to_linear("pending"), "pending")
        self.assertEqual(map_hermes_todo_status_to_linear("in_progress"), "inProgress")
        self.assertEqual(map_hermes_todo_status_to_linear("in-progress"), "inProgress")
        self.assertEqual(map_hermes_todo_status_to_linear("completed"), "completed")
        self.assertEqual(map_hermes_todo_status_to_linear("cancelled"), "canceled")
        self.assertEqual(map_hermes_todo_status_to_linear("unknown"), "pending")

    def test_todos_to_plan_steps(self) -> None:
        todos = [
            {"content": "Review project context", "status": "completed"},
            {"content": "SSH to staging", "status": "in_progress"},
            {"content": "Run tests", "status": "pending"},
            {"content": "", "status": "pending"},
        ]
        steps = hermes_todos_to_linear_plan_steps(todos)
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0]["content"], "Review project context")
        self.assertEqual(steps[0]["status"], "completed")
        self.assertEqual(steps[1]["status"], "inProgress")
        self.assertEqual(steps[2]["status"], "pending")

    def test_truncates_long_step_content(self) -> None:
        long_text = "x" * 60
        steps = hermes_todos_to_linear_plan_steps(
            [{"content": long_text, "status": "pending"}],
        )
        self.assertEqual(len(steps[0]["content"]), 48)
        self.assertTrue(steps[0]["content"].endswith("…"))


class TestProjectContextParsing(unittest.TestCase):
    def test_parse_project_from_prompt_context(self) -> None:
        xml = (
            '<issue identifier="PLY-1">'
            '<project name="Home Lab">VPS and staging hosts</project>'
            "</issue>"
        )
        parsed = parse_prompt_context(xml)
        self.assertEqual(parsed["project_name"], "Home Lab")
        self.assertEqual(parsed["project_summary"], "VPS and staging hosts")

    def test_format_project_context_block(self) -> None:
        block = format_project_context_block({
            "name": "Home Lab",
            "description": "Primary infra",
            "status": {"name": "In Progress", "type": "started"},
            "url": "https://linear.app/project/home-lab",
        })
        self.assertIn("Project: Home Lab", block)
        self.assertIn("Project status: In Progress (started)", block)
        self.assertIn("Project summary: Primary infra", block)
        self.assertIn("Project URL:", block)


class TestBuildNativeTurnMessage(unittest.TestCase):
    def setUp(self) -> None:
        self.processor = TaskProcessor(linear=MagicMock())
        self.session = AgentSession(
            session_id="sess-1",
            issue_id="issue-1",
            issue_identifier="PLY-78",
            action=SessionAction.created,
            prompt_context="",
            title="Richer context",
            description="Issue body",
            guidance=["Always read project overview first"],
            project_name="Home Lab",
            project_summary="From promptContext",
        )
        self.issue = {
            "identifier": "PLY-78",
            "title": "Richer context",
            "description": "Issue body",
            "state": {"name": "In Progress"},
            "team": {"name": "Platform", "key": "PLY"},
            "labels": {"nodes": [{"name": "agent"}]},
            "project": {
                "name": "Home Lab",
                "description": "From API",
            },
        }

    def test_created_turn_includes_full_context(self) -> None:
        siblings = format_project_issues_block([
            {
                "identifier": "PLY-77",
                "title": "Sibling issue",
                "state": {"name": "Done"},
                "description": "",
            },
        ])
        msg = self.processor.build_native_turn_message(
            self.session,
            self.issue,
            "Fix the SSH target",
            thread_context=(
                f"{THREAD_CONTEXT_HEADER}\n"
                "User: earlier note\n"
                f"{THREAD_CONTEXT_FOOTER}"
            ),
            project_issues_block=siblings,
            include_full_context=True,
        )
        self.assertIn("Linear assignment: PLY-78", msg)
        self.assertIn("Project: Home Lab", msg)
        self.assertIn("Team/workspace guidance:", msg)
        self.assertIn("Recent issues in this project", msg)
        self.assertIn("Fix the SSH target", msg)
        self.assertIn(THREAD_CONTEXT_HEADER, msg)
        self.assertIn(HERMES_NATIVE_TODO_HINT, msg)
        self.assertIn("Confirm target hosts", msg)
        self.assertIn(LINEAR_OUTPUT_RULES.strip()[:40], msg)
        self.assertNotIn("capabilities", msg.lower())

    def test_prompted_turn_is_delta_only(self) -> None:
        self.session.action = SessionAction.prompted
        msg = self.processor.build_native_turn_message(
            self.session,
            self.issue,
            "Any update?",
            thread_context=(
                f"{THREAD_CONTEXT_HEADER}\n"
                "User: follow-up thread\n"
                f"{THREAD_CONTEXT_FOOTER}"
            ),
            include_full_context=False,
        )
        self.assertIn("[Replying on Linear issue PLY-78", msg)
        self.assertIn("Any update?", msg)
        self.assertIn(THREAD_CONTEXT_HEADER, msg)
        self.assertNotIn("Issue description:", msg)
        self.assertNotIn("Team/workspace guidance:", msg)
        self.assertNotIn(HERMES_NATIVE_TODO_HINT, msg)
        self.assertNotIn(LINEAR_OUTPUT_RULES, msg)


class TestHermesNativeSettings(unittest.TestCase):
    def test_default_off(self) -> None:
        os.environ.pop("HERMES_NATIVE_MODE", None)
        self.assertFalse(Settings().hermes_native_mode)

    def test_env_enables_flag(self) -> None:
        os.environ["HERMES_NATIVE_MODE"] = "1"
        try:
            self.assertTrue(Settings().hermes_native_mode)
        finally:
            os.environ["HERMES_NATIVE_MODE"] = "0"


class TestSessionPlanSync(unittest.IsolatedAsyncioTestCase):
    async def test_sync_from_hermes_todos_updates_linear(self) -> None:
        linear = MagicMock()
        linear.update_plan = AsyncMock()
        tracker = SessionPlanTracker(session_id="sess-1", linear=linear)
        todos = [
            {"content": "Step one", "status": "completed"},
            {"content": "Step two", "status": "in_progress"},
        ]
        await tracker.sync_from_hermes_todos(todos)
        self.assertEqual(len(tracker.steps), 2)
        linear.update_plan.assert_awaited_once()
        sent_steps = linear.update_plan.await_args.args[1]
        self.assertEqual(sent_steps[0]["status"], "completed")
        self.assertEqual(sent_steps[1]["status"], "inProgress")


if __name__ == "__main__":
    unittest.main()
