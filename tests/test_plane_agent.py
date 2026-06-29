"""Basic tests for plane_agent helpers (no live API)."""

import os
from pathlib import Path

# plane_agent asserts credentials at import time
os.environ.setdefault("PLANE_API_KEY", "test-token")
os.environ.setdefault("PLANE_WEBHOOK_SECRET", "test-secret")

from plane_agent import (  # noqa: E402
    build_plane_consent_url,
    extract_github_pr_urls,
    format_hermes_tool_progress,
    update_env_file,
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


def test_build_plane_consent_url():
    url = build_plane_consent_url(
        client_id="client-123",
        redirect_uri="https://plane.example.com/plane/oauth/callback",
        api_url="https://api.plane.so",
    )
    assert url.startswith("https://api.plane.so/auth/o/authorize-app/?")
    assert "client_id=client-123" in url
    assert "response_type=code" in url
    assert "redirect_uri=https%3A%2F%2Fplane.example.com%2Fplane%2Foauth%2Fcallback" in url


def test_update_env_file_upserts_values(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("PLANE_WEBHOOK_SECRET=old\nOTHER=value\n", encoding="utf-8")

    update_env_file(
        env_path,
        {
            "PLANE_API_KEY": "new-token",
            "PLANE_APP_INSTALLATION_ID": "install-1",
        },
    )

    content = env_path.read_text(encoding="utf-8")
    assert "PLANE_WEBHOOK_SECRET=old" in content
    assert "OTHER=value" in content
    assert "PLANE_API_KEY=new-token" in content
    assert "PLANE_APP_INSTALLATION_ID=install-1" in content
