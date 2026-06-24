"""Tests for GitHub PR URL extraction used by linear-agent to link PRs to Linear sessions.

When Hermes creates a PR, linear-agent extracts the URL from the response text
so it can attach it to the agent session via addedExternalUrls. Linear then
renders the PR diff in its Reviews UI.
"""

import os
import unittest

os.environ.setdefault("LINEAR_API_KEY", "test-key")
os.environ.setdefault("LINEAR_WEBHOOK_SECRET", "test-secret")

from linear_agent import (
    _pr_external_url_label,
    extract_github_pr_urls,
)


class TestExtractGithubPrUrls(unittest.TestCase):
    def test_single_url(self) -> None:
        text = "Opened https://github.com/org/repo/pull/42 for review."
        self.assertEqual(
            extract_github_pr_urls(text),
            ["https://github.com/org/repo/pull/42"],
        )

    def test_deduplicates(self) -> None:
        url = "https://github.com/org/repo/pull/7"
        self.assertEqual(
            extract_github_pr_urls(url, f"See {url} again"),
            [url],
        )

    def test_strips_trailing_punctuation(self) -> None:
        text = "(https://github.com/org/repo/pull/3)."
        self.assertEqual(
            extract_github_pr_urls(text),
            ["https://github.com/org/repo/pull/3"],
        )

    def test_pr_label(self) -> None:
        self.assertEqual(
            _pr_external_url_label("https://github.com/org/repo/pull/99"),
            "PR #99",
        )


if __name__ == "__main__":
    unittest.main()
