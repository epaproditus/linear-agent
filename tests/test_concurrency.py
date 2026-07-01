"""PLY-79: Validate Hermes/linear-agent concurrency and multi-issue behavior."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("LINEAR_API_KEY", "test-key")
os.environ.setdefault("LINEAR_WEBHOOK_SECRET", "test-secret")

from linear_agent import (  # noqa: E402
    MAX_CONCURRENT_SESSIONS,
    RATE_LIMIT_MAX_REQUESTS,
    AgentSession,
    AgentWebhookHandler,
    SessionAction,
    SlidingWindowRateLimiter,
    TaskProcessor,
)


def _agent_session_payload(
    session_id: str,
    issue_id: str = "issue-1",
    identifier: str = "PLY-1",
) -> dict[str, Any]:
    """Minimal AgentSessionEvent.created payload."""
    return {
        "type": "AgentSessionEvent",
        "action": "created",
        "agentSession": {
            "id": session_id,
            "issue": {
                "id": issue_id,
                "identifier": identifier,
                "title": "Test issue",
                "description": "Do the thing",
                "team": {"id": "team-1", "key": "PLY"},
            },
            "comment": {"body": "@Hermes please investigate"},
        },
        "promptContext": "",
        "previousComments": [],
        "agentActivity": {},
    }


class _TrackingProcessor(TaskProcessor):
    """TaskProcessor stub that records concurrent execution."""

    def __init__(self) -> None:
        super().__init__(linear=MagicMock())  # type: ignore[arg-type]
        self.active = 0
        self.max_active = 0
        self.start_times: list[float] = []
        self.end_times: list[float] = []
        self.sleep_s = 0.2

    async def process(
        self,
        session: AgentSession,
        issue: dict[str, Any] | None,
    ) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.start_times.append(time.monotonic())
        try:
            await asyncio.sleep(self.sleep_s)
        finally:
            self.end_times.append(time.monotonic())
            self.active -= 1


@pytest.fixture
def handler() -> tuple[AgentWebhookHandler, _TrackingProcessor]:
    processor = _TrackingProcessor()
    linear = MagicMock()
    linear.get_issue = AsyncMock(
        return_value={
            "id": "issue-1",
            "identifier": "PLY-1",
            "title": "Test",
            "description": "",
            "team": {"id": "team-1", "key": "PLY"},
            "state": {"type": "unstarted", "name": "Todo"},
            "labels": {"nodes": []},
            "comments": {"nodes": []},
        }
    )
    linear.send_error = AsyncMock()
    h = AgentWebhookHandler(linear=linear, processor=processor)
    return h, processor


@pytest.mark.asyncio
async def test_multiple_sessions_run_concurrently_not_sequentially(
    handler: tuple[AgentWebhookHandler, _TrackingProcessor],
) -> None:
    """Different session IDs should overlap in wall-clock time."""
    h, processor = handler
    processor.sleep_s = 0.15
    n = 5

    start = time.monotonic()
    statuses = await asyncio.gather(
        *[
            h.handle_event(_agent_session_payload(f"sess-{i}", f"issue-{i}", f"PLY-{i}"))
            for i in range(n)
        ]
    )
    # Wait for background tasks
    await asyncio.sleep(0.05)
    while h._active_runs:
        await asyncio.sleep(0.05)

    elapsed = time.monotonic() - start
    assert all("processing" in s for s in statuses)
    assert processor.max_active > 1, "sessions should overlap, not run one-at-a-time"
    # Sequential would take n * sleep_s; parallel should be much faster
    assert elapsed < processor.sleep_s * n * 0.8


@pytest.mark.asyncio
async def test_semaphore_caps_concurrent_sessions(
    handler: tuple[AgentWebhookHandler, _TrackingProcessor],
) -> None:
    """At most MAX_CONCURRENT_SESSIONS handlers run process() at once."""
    h, processor = handler
    processor.sleep_s = 0.3
    n = MAX_CONCURRENT_SESSIONS + 3

    await asyncio.gather(
        *[
            h.handle_event(_agent_session_payload(f"sess-{i}", f"issue-{i}"))
            for i in range(n)
        ]
    )
    # Let tasks start
    await asyncio.sleep(0.05)
    assert processor.max_active <= MAX_CONCURRENT_SESSIONS
    assert processor.max_active == MAX_CONCURRENT_SESSIONS

    # Drain all tasks
    deadline = time.monotonic() + 5.0
    while h._active_runs and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not h._active_runs


@pytest.mark.asyncio
async def test_same_session_rejected_while_running(
    handler: tuple[AgentWebhookHandler, _TrackingProcessor],
) -> None:
    """Follow-up AgentSessionEvent while a session is active returns 'already running'.

    Note: identical webhook retries are deduped first; a distinct prompted event
    with a new activity ID exercises the _active_runs guard.
    """
    h, processor = handler
    processor.sleep_s = 0.5
    session_id = "sess-dup"

    first = await h.handle_event(_agent_session_payload(session_id))
    assert "processing" in first

    follow_up = {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "agentSession": {
            "id": session_id,
            "issue": {
                "id": "issue-1",
                "identifier": "PLY-1",
                "title": "Test issue",
                "team": {"id": "team-1", "key": "PLY"},
            },
        },
        "agentActivity": {"id": "act-follow-up", "body": "Any update?"},
        "promptContext": "",
        "previousComments": [],
    }
    second = await h.handle_event(follow_up)
    assert second == "already running"

    while h._active_runs:
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_comment_path_uses_semaphore_but_not_active_runs(
    handler: tuple[AgentWebhookHandler, _TrackingProcessor],
) -> None:
    """@mention path spawns work via _process_with_semaphore without _active_runs tracking."""
    h, processor = handler
    processor.sleep_s = 0.2
    linear = h.linear
    linear._gql = AsyncMock(
        return_value={
            "agentSessionCreateOnIssue": {
                "success": True,
                "agentSession": {"id": "comment-sess-1"},
            }
        }
    )

    payload = {
        "type": "Comment",
        "action": "create",
        "notification": {
            "comment": {"body": "@Hermes look at this"},
            "issueId": "issue-1",
            "commentId": "comment-1",
        },
    }

    status = await h.handle_event(payload)
    assert "processing @mention" in status
    assert "comment-sess-1" not in h._active_runs

    await asyncio.sleep(0.05)
    assert processor.max_active >= 1

    await asyncio.sleep(processor.sleep_s + 0.1)


def test_sliding_window_rate_limiter_rejects_burst() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=5, window_s=60.0)
    allowed = [limiter.allow() for _ in range(6)]
    assert allowed[:5] == [True] * 5
    assert allowed[5] is False


def test_sliding_window_rate_limiter_prunes_expired() -> None:
    limiter = SlidingWindowRateLimiter(max_requests=2, window_s=0.05)
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False
    time.sleep(0.06)
    assert limiter.allow() is True


@pytest.mark.asyncio
async def test_stop_signal_cancels_active_session(
    handler: tuple[AgentWebhookHandler, _TrackingProcessor],
) -> None:
    """Stop signal cancels the background task registered in _active_runs."""
    h, processor = handler
    processor.sleep_s = 2.0
    session_id = "sess-stop"

    await h.handle_event(_agent_session_payload(session_id))
    await asyncio.sleep(0.05)
    assert session_id in h._active_runs

    stop_payload = {
        "type": "AgentSessionEvent",
        "action": "prompted",
        "agentSession": {
            "id": session_id,
            "issue": {"id": "issue-1", "identifier": "PLY-1"},
        },
        "agentActivity": {"id": "act-stop", "signal": "stop"},
        "promptContext": "",
    }
    h.linear.send_response = AsyncMock()
    status = await h.handle_event(stop_payload)
    assert "stopped" in status

    await asyncio.sleep(0.1)
    assert session_id not in h._active_runs


@pytest.mark.asyncio
async def test_dedup_drops_duplicate_webhook_within_ttl(
    handler: tuple[AgentWebhookHandler, _TrackingProcessor],
) -> None:
    h, _processor = handler
    payload = _agent_session_payload("sess-dedup")

    first = await h.handle_event(payload)
    second = await h.handle_event(payload)

    assert "processing" in first
    assert second == "deduped"
