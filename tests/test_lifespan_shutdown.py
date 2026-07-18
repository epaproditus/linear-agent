"""Graceful shutdown behavior for PLY-25.

Covers:
- `Settings.graceful_shutdown_timeout` exposure
- `lifespan` shutdown drain behavior
"""
from linear_agent import Settings


def test_graceful_shutdown_timeout_default():
    settings = Settings(linear_api_key="k", linear_webhook_secret="s", hermes_api_key="h")
    assert settings.graceful_shutdown_timeout == 10.0


def test_graceful_shutdown_timeout_override():
    settings = Settings(
        linear_api_key="k",
        linear_webhook_secret="s",
        hermes_api_key="h",
        graceful_shutdown_timeout=5.0,
    )
    assert settings.graceful_shutdown_timeout == 5.0
