"""Basic tests for plane_agent helpers (no live API)."""

import os

# plane_agent asserts credentials at import time
os.environ.setdefault("PLANE_API_KEY", "test-token")
os.environ.setdefault("PLANE_WEBHOOK_SECRET", "test-secret")

from plane_agent import (  # noqa: E402
    extract_github_pr_urls,
    format_hermes_tool_progress,
    verify_hmac,
)


def test_verify_hmac_valid():
    body = b'{"type":"agent_run"}'
    secret = "shh"
    import hmac
    import hashlib

    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_hmac(body, sig, secret)


def test_verify_hmac_invalid():
    assert not verify_hmac(b"{}", "bad", "secret")


def test_format_hermes_tool_progress_read_file():
    text = format_hermes_tool_progress(
        {
            "tool": "read_file",
            "status": "running",
            "input": {"path": "/workspace/plane_agent.py"},
        }
    )
    assert text == "Read `plane_agent.py`"


def test_extract_github_pr_urls():
    urls = extract_github_pr_urls(
        "See https://github.com/org/repo/pull/11 for details."
    )
    assert urls == ["https://github.com/org/repo/pull/11"]
