"""Thin run registry and tool audit trail for Linear agent sessions."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class RunState(str, Enum):
    created = "created"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_run_id(run_id: str) -> str:
    return run_id.replace("-", "")[:8]


@dataclass
class RunEvent:
    """Structured tool or lifecycle event on a run."""

    timestamp: str
    event_type: str
    tool_name: str | None = None
    status: str | None = None
    summary: str | None = None
    payload: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
        }
        if self.tool_name is not None:
            data["tool_name"] = self.tool_name
        if self.status is not None:
            data["status"] = self.status
        if self.summary is not None:
            data["summary"] = self.summary
        if self.payload is not None:
            data["payload"] = self.payload
        return data


@dataclass
class RunRecord:
    run_id: str
    state: RunState
    trigger: str
    linear_session_id: str | None = None
    issue_id: str | None = None
    issue_identifier: str | None = None
    hermes_session_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    events: list[RunEvent] = field(default_factory=list)

    def to_dict(self, *, include_events: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "run_short_id": _short_run_id(self.run_id),
            "state": self.state.value,
            "trigger": self.trigger,
            "linear_session_id": self.linear_session_id,
            "issue_id": self.issue_id,
            "issue_identifier": self.issue_identifier,
            "hermes_session_id": self.hermes_session_id,
            "error": self.error,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }
        if include_events:
            data["events"] = [event.to_dict() for event in self.events]
        return data


def tool_progress_to_audit_event(
    progress_data: dict[str, Any],
    *,
    summary: str | None = None,
) -> RunEvent:
    """Build a structured audit event from a Hermes tool progress SSE payload."""
    tool = (progress_data.get("tool") or progress_data.get("name") or "").strip()
    status = (progress_data.get("status") or "running").lower()
    label = (progress_data.get("label") or "").strip()
    tool_call_id = progress_data.get("toolCallId") or progress_data.get("tool_call_id")

    payload: dict[str, Any] = {}
    if tool_call_id:
        payload["tool_call_id"] = tool_call_id
    if label:
        payload["label"] = label

    return RunEvent(
        timestamp=_utc_now(),
        event_type="tool",
        tool_name=tool or None,
        status=status or None,
        summary=summary,
        payload=payload or None,
    )


class RunRegistry:
    """SQLite-backed run registry with append-only tool audit events."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS runs (
                        run_id TEXT PRIMARY KEY,
                        state TEXT NOT NULL,
                        trigger TEXT NOT NULL,
                        linear_session_id TEXT,
                        issue_id TEXT,
                        issue_identifier TEXT,
                        hermes_session_id TEXT,
                        error TEXT,
                        metadata TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        started_at TEXT,
                        ended_at TEXT
                    );
                    CREATE TABLE IF NOT EXISTS run_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        tool_name TEXT,
                        status TEXT,
                        summary TEXT,
                        payload TEXT,
                        FOREIGN KEY (run_id) REFERENCES runs(run_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_runs_linear_session
                        ON runs(linear_session_id);
                    CREATE INDEX IF NOT EXISTS idx_runs_issue
                        ON runs(issue_id);
                    CREATE INDEX IF NOT EXISTS idx_runs_created_at
                        ON runs(created_at);
                    CREATE INDEX IF NOT EXISTS idx_run_events_run_id
                        ON run_events(run_id);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def create_run(
        self,
        *,
        trigger: str,
        linear_session_id: str | None = None,
        issue_id: str | None = None,
        issue_identifier: str | None = None,
        hermes_session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        created_at = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, state, trigger, linear_session_id, issue_id,
                        issue_identifier, hermes_session_id, metadata,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        RunState.created.value,
                        trigger,
                        linear_session_id,
                        issue_id,
                        issue_identifier,
                        hermes_session_id or linear_session_id,
                        json.dumps(metadata or {}),
                        created_at,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return run_id

    def transition(
        self,
        run_id: str,
        state: RunState,
        *,
        error: str | None = None,
        metadata_patch: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT metadata, started_at FROM runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"run not found: {run_id}")

                metadata = json.loads(row["metadata"] or "{}")
                if metadata_patch:
                    metadata.update(metadata_patch)

                started_at = row["started_at"]
                ended_at: str | None = None
                if state == RunState.running and not started_at:
                    started_at = now
                if state in (
                    RunState.completed,
                    RunState.failed,
                    RunState.cancelled,
                ):
                    ended_at = now

                conn.execute(
                    """
                    UPDATE runs
                    SET state = ?, error = ?, metadata = ?,
                        started_at = COALESCE(?, started_at), ended_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        state.value,
                        error,
                        json.dumps(metadata),
                        started_at,
                        ended_at,
                        run_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def append_event(self, run_id: str, event: RunEvent) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO run_events (
                        run_id, timestamp, event_type, tool_name, status,
                        summary, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        event.timestamp,
                        event.event_type,
                        event.tool_name,
                        event.status,
                        event.summary,
                        json.dumps(event.payload) if event.payload else None,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,),
                ).fetchone()
                if row is None:
                    return None
                events = self._fetch_events(conn, run_id)
                return self._row_to_record(row, events)
            finally:
                conn.close()

    def list_runs(
        self,
        *,
        state: str | None = None,
        issue_id: str | None = None,
        linear_session_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if issue_id:
            clauses.append("issue_id = ?")
            params.append(issue_id)
        if linear_session_id:
            clauses.append("linear_session_id = ?")
            params.append(linear_session_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([limit, offset])

        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"""
                    SELECT * FROM runs
                    {where}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                ).fetchall()
                return [
                    self._row_to_record(row, events=[])
                    for row in rows
                ]
            finally:
                conn.close()

    def _fetch_events(
        self, conn: sqlite3.Connection, run_id: str,
    ) -> list[RunEvent]:
        rows = conn.execute(
            """
            SELECT timestamp, event_type, tool_name, status, summary, payload
            FROM run_events
            WHERE run_id = ?
            ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()
        events: list[RunEvent] = []
        for row in rows:
            payload = json.loads(row["payload"]) if row["payload"] else None
            events.append(
                RunEvent(
                    timestamp=row["timestamp"],
                    event_type=row["event_type"],
                    tool_name=row["tool_name"],
                    status=row["status"],
                    summary=row["summary"],
                    payload=payload,
                ),
            )
        return events

    @staticmethod
    def _row_to_record(
        row: sqlite3.Row,
        events: list[RunEvent],
    ) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            state=RunState(row["state"]),
            trigger=row["trigger"],
            linear_session_id=row["linear_session_id"],
            issue_id=row["issue_id"],
            issue_identifier=row["issue_identifier"],
            hermes_session_id=row["hermes_session_id"],
            error=row["error"],
            metadata=json.loads(row["metadata"] or "{}"),
            created_at=row["created_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            events=events,
        )
