"""
Fetch a GitHub issue and its repository metadata.

Takes a GitHub issue URL (e.g. https://github.com/owner/repo/issues/123)
and returns all the information needed to run the agent:
  - issue title + body (combined as issue_text)
  - repo URL for cloning
  - the commit SHA to check out (the commit just before the issue was opened,
    or more practically, the HEAD of the default branch)

For SWE-bench evaluation, the base_commit is provided by the dataset.
For live GitHub issues (demo mode), we use the current HEAD of the default branch.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from github import Github
from tenacity import retry, stop_after_attempt, wait_exponential


@dataclass
class IssueData:
    """All data needed to run the agent on a GitHub issue."""

    issue_url: str
    repo_url: str  # HTTPS clone URL
    repo_full_name: str  # "owner/repo"
    issue_number: int
    issue_title: str
    issue_body: str
    base_commit: str  # SHA to check out
    branch: str  # Default branch name
    labels: list[str]

    @property
    def issue_text(self) -> str:
        """Combined issue title + body for the agent prompt."""
        return f"Title: {self.issue_title}\n\n{self.issue_body}"


def parse_github_url(url: str) -> tuple[str, int]:
    """
    Parse a GitHub issue URL into (repo_full_name, issue_number).

    Accepts:
        https://github.com/owner/repo/issues/123
        github.com/owner/repo/issues/123
        owner/repo#123
    """
    # Full URL format
    match = re.search(r"github\.com/([^/]+/[^/]+)/issues/(\d+)", url)
    if match:
        return match.group(1), int(match.group(2))

    # Short format: owner/repo#123
    match = re.match(r"^([^/]+/[^/]+)#(\d+)$", url.strip())
    if match:
        return match.group(1), int(match.group(2))

    raise ValueError(
        f"Cannot parse GitHub issue URL: {url!r}\n"
        "Expected format: https://github.com/owner/repo/issues/123"
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def fetch_issue(
    url: str,
    token: str | None = None,
) -> IssueData:
    """
    Fetch a GitHub issue by URL.

    Args:
        url:   GitHub issue URL (any of the formats accepted by parse_github_url)
        token: GitHub personal access token. Falls back to GITHUB_TOKEN env var.
               If not provided, uses the public API (rate-limited to 60 req/hr).

    Returns:
        IssueData with all information needed to run the agent.

    Raises:
        ValueError: If the URL cannot be parsed.
        GithubException: If the issue or repo cannot be fetched.
    """
    token = token or os.getenv("GITHUB_TOKEN")
    gh = Github(token) if token else Github()

    repo_full_name, issue_number = parse_github_url(url)

    repo = gh.get_repo(repo_full_name)
    issue = repo.get_issue(issue_number)

    # Get the current HEAD commit of the default branch
    default_branch = repo.default_branch
    branch_ref = repo.get_branch(default_branch)
    head_commit = branch_ref.commit.sha

    labels = [label.name for label in issue.labels]

    return IssueData(
        issue_url=url,
        repo_url=repo.clone_url,
        repo_full_name=repo_full_name,
        issue_number=issue_number,
        issue_title=issue.title,
        issue_body=issue.body or "",
        base_commit=head_commit,
        branch=default_branch,
        labels=labels,
    )
