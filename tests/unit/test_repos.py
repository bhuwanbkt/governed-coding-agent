from __future__ import annotations

import pytest

from coding_agent.governance.repos import repo_url_problem


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/bhuwanbkt/agent-sandbox-repo.git",
        "https://github.com/bhuwanbkt/agent-sandbox-repo",
        "https://github.com/bhuwanbkt/agent-sandbox-repo/",
        "https://gitlab.example.com/team/service.v2",
        "file:///tmp/remote.git",
    ],
)
def test_repository_urls_are_accepted(url: str) -> None:
    assert repo_url_problem(url) is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://github.com/bhuwanbkt/", "one repository"),  # the allowlist prefix (seen in production)
        ("https://github.com/bhuwanbkt", "one repository"),
        ("https://github.com/", "one repository"),
        ("https://github.com/o/r/tree/main", "one repository"),
        ("https://github.com/o/r?tab=readme", "query"),
        ("https://github.com/o/r#readme", "query"),
        ("https://user:pw@github.com/o/r.git", "credentials"),
        ("https://x-access-token@github.com/o/r.git", "credentials"),
        ("http://github.com/o/r.git", "only https"),
        ("git@github.com:o/r.git", "only https"),
        ("ssh://git@github.com/o/r.git", "only https"),
        ("https://github.com/o/..", "aren't allowed"),
        ("https://github.com/-o/r", "aren't allowed"),
        ("https://github.com/o/r%20x", "aren't allowed"),
        ("file:///", "repository folder"),
        ("file:///tmp/", "repository folder"),
        ("file://host/tmp/r.git", "repository folder"),
    ],
)
def test_non_repository_urls_are_refused(url: str, reason: str) -> None:
    problem = repo_url_problem(url)
    assert problem is not None and reason in problem
