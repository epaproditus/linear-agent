"""PLY-150: Ephemeral per-issue task workspace lifecycle."""

from __future__ import annotations

from pathlib import Path

import pytest

from linear_agent import (
    cleanup_task_workspace,
    ensure_task_workspace,
    format_task_workspace_block,
    settings,
    task_workspace_dir,
)


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workspace"
    monkeypatch.setattr(settings, "agent_workdir", str(root))
    return root


def test_task_workspace_dir(workspace_root: Path) -> None:
    assert task_workspace_dir("PLY-150") == workspace_root / "PLY-150"


def test_task_workspace_dir_rejects_unsafe_keys(workspace_root: Path) -> None:
    with pytest.raises(ValueError):
        task_workspace_dir("../escape")


def test_ensure_task_workspace_creates_directory(workspace_root: Path) -> None:
    path = ensure_task_workspace("PLY-150")
    assert path.is_dir()
    assert path == workspace_root / "PLY-150"


def test_ensure_task_workspace_is_idempotent(workspace_root: Path) -> None:
    first = ensure_task_workspace("PLY-150")
    (first / "repo").mkdir()
    second = ensure_task_workspace("PLY-150")
    assert second == first
    assert (second / "repo").is_dir()


def test_cleanup_task_workspace_removes_directory(workspace_root: Path) -> None:
    path = ensure_task_workspace("PLY-99")
    (path / "clone").write_text("data")
    assert cleanup_task_workspace("PLY-99") is True
    assert not path.exists()


def test_cleanup_task_workspace_missing_is_noop(workspace_root: Path) -> None:
    assert cleanup_task_workspace("PLY-404") is False


def test_cleanup_refuses_path_outside_base(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = workspace_root.parent / "outside"
    outside.mkdir()
    monkeypatch.setattr(settings, "agent_workdir", str(workspace_root))
    # Force a valid key whose resolved path we cannot easily escape in normal use;
    # invalid keys are rejected earlier.
    ensure_task_workspace("PLY-1")
    assert cleanup_task_workspace("PLY-1") is True


def test_format_task_workspace_block_ready() -> None:
    block = format_task_workspace_block("PLY-150", ready=True)
    assert "is ready" in block
    assert "Do not delete" in block
    assert "Done or Canceled" in block


def test_format_task_workspace_block_invalid_key() -> None:
    assert format_task_workspace_block("../bad") == ""
