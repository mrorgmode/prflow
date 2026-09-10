"""Read-only GitHub adapter. Every call goes through the `gh` executable.

Phase 1 performs no GitHub mutations; `graphql()` refuses mutation documents so a
later write path has to be added deliberately and auditably.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .errors import PrflowError

GH_TIMEOUT = 120
THREADS_PAGE_SIZE = 50
COMMENTS_PAGE_SIZE = 100

PR_FIELDS = "id number url title state isDraft headRefName baseRefName headRefOid isCrossRepository"
THREAD_FIELDS = (
    "id isResolved isOutdated path line originalLine startLine originalStartLine diffSide subjectType resolvedBy { login }"
)
COMMENT_FIELDS = "id databaseId url author { login } body createdAt updatedAt diffHunk"

PR_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{ {PR_FIELDS} }}
  }}
}}
"""

THREADS_QUERY = f"""
query($owner: String!, $name: String!, $number: Int!, $threadsFirst: Int!, $commentsFirst: Int!, $cursor: String) {{
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      {PR_FIELDS}
      reviewThreads(first: $threadsFirst, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          {THREAD_FIELDS}
          comments(first: $commentsFirst) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{ {COMMENT_FIELDS} }}
          }}
        }}
      }}
    }}
  }}
}}
"""

COMMENTS_QUERY = f"""
query($id: ID!, $commentsFirst: Int!, $cursor: String) {{
  node(id: $id) {{
    ... on PullRequestReviewThread {{
      comments(first: $commentsFirst, after: $cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ {COMMENT_FIELDS} }}
      }}
    }}
  }}
}}
"""

PR_LIST_FIELDS = "number,url,title,headRefName,baseRefName,headRefOid,isDraft,isCrossRepository,headRepositoryOwner"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TOKEN_RE = re.compile(r"(gh[opsur]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})")


def redact(text: str) -> str:
    return _TOKEN_RE.sub("[REDACTED]", text)


def _gh_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"})
    return env


def _classify_failure(args: list[str], stderr: str) -> PrflowError:
    text = redact(stderr.strip())[:500]
    low = text.lower()
    what = f"gh {' '.join(args[:2])}"
    if "gh auth login" in low or "not logged in" in low or "http 401" in low or "bad credentials" in low:
        return PrflowError(
            "gh_auth",
            f"{what}: GitHub CLI is not authenticated: {text}",
            hint="run `gh auth login`; prflow never guesses or switches credentials",
        )
    if "could not resolve to a pullrequest" in low:
        return PrflowError("pr_not_found", f"{what}: {text}", hint="check the --pr number")
    if "could not resolve to a repository" in low:
        return PrflowError("repo_not_found", f"{what}: {text}", hint="check --repo, or that your gh account can read it")
    if "git remotes" in low or "set-default" in low or "not a git repository" in low:
        return PrflowError(
            "repo_unresolved",
            f"{what}: cannot determine the GitHub repository: {text}",
            hint="pass --repo OWNER/REPO or run `gh repo set-default`",
        )
    return PrflowError("gh_failed", f"{what} failed: {text}")


def run_gh(args: list[str], cwd: Path | str) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            env=_gh_env(),
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise PrflowError("gh_missing", "GitHub CLI `gh` not found on PATH", hint="install it from https://cli.github.com") from None
    except subprocess.TimeoutExpired:
        raise PrflowError("gh_failed", f"gh {' '.join(args[:2])} timed out after {GH_TIMEOUT}s") from None
    if proc.returncode != 0:
        raise _classify_failure(args, proc.stderr)
    return proc.stdout


def gh_json(args: list[str], cwd: Path | str) -> Any:
    out = run_gh(args, cwd)
    try:
        return json.loads(out)
    except ValueError:
        raise PrflowError("gh_failed", f"gh {' '.join(args[:2])} returned non-JSON output") from None


def graphql(query: str, variables: dict[str, str | int], cwd: Path | str) -> dict[str, Any]:
    if query.lstrip().startswith("mutation"):
        raise AssertionError("Phase 1 prflow performs no GitHub mutations")
    argv = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # Strings always use raw -f: -F would treat a leading "@" as a file to read.
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError(f"unsupported GraphQL variable {key}={value!r}")
        argv.extend(["-F" if isinstance(value, int) else "-f", f"{key}={value}"])
    data = gh_json(argv, cwd)
    if not isinstance(data, dict):
        raise _classify_failure(argv, "unexpected GraphQL response: expected an object")
    if data.get("errors") or not isinstance(data.get("data"), dict):
        messages = "; ".join(str(e.get("message")) for e in (data.get("errors") or []) if isinstance(e, dict))
        raise _classify_failure(argv, messages or "unexpected GraphQL response")
    return data["data"]


def resolve_repo(cwd: Path | str, explicit: str | None = None) -> str:
    """Canonical OWNER/REPO: explicit --repo, else gh's resolution from this checkout's remotes."""
    args = ["repo", "view"]
    if explicit is not None:
        if not REPO_RE.match(explicit):
            raise PrflowError("usage", f"--repo must look like OWNER/REPO, got {explicit!r}")
        args.append(explicit)
    data = gh_json([*args, "--json", "nameWithOwner,url"], cwd)
    return data["nameWithOwner"]


def open_prs_for_head(repo: str, head: str, cwd: Path | str) -> list[dict[str, Any]]:
    return gh_json(
        ["pr", "list", f"--repo={repo}", f"--head={head}", "--state=open", "--json", PR_LIST_FIELDS, "--limit", "30"], cwd
    )


def open_prs(repo: str, cwd: Path | str) -> list[dict[str, Any]]:
    return gh_json(
        ["pr", "list", f"--repo={repo}", "--state=open", "--json", "number,headRefName,baseRefName,isCrossRepository", "--limit", "200"],
        cwd,
    )


def _pull_request(data: dict[str, Any], repo: str, number: int) -> dict[str, Any]:
    repository = data.get("repository")
    if repository is None:
        raise PrflowError("repo_not_found", f"repository {repo} not found or not readable")
    pull = repository.get("pullRequest")
    if pull is None:
        raise PrflowError("pr_not_found", f"pull request #{number} not found in {repo}")
    return pull


def fetch_pr(repo: str, number: int, cwd: Path | str) -> dict[str, Any]:
    owner, name = repo.split("/", 1)
    return _pull_request(graphql(PR_QUERY, {"owner": owner, "name": name, "number": number}, cwd), repo, number)


def _next_cursor(page_info: dict[str, Any], previous: str | None) -> str | None:
    if not page_info.get("hasNextPage"):
        return None
    cursor = page_info.get("endCursor")
    if not cursor or cursor == previous:
        raise PrflowError("gh_failed", "GitHub pagination did not advance; refusing to return a partial snapshot")
    return cursor


def _remaining_comments(thread_id: str, cursor: str, cwd: Path | str) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    while cursor:
        data = graphql(COMMENTS_QUERY, {"id": thread_id, "commentsFirst": COMMENTS_PAGE_SIZE, "cursor": cursor}, cwd)
        node = data.get("node")
        if not node or "comments" not in node:
            raise PrflowError("gh_failed", f"review thread {thread_id} vanished while paging its comments")
        comments.extend(c for c in node["comments"]["nodes"] if c)
        cursor = _next_cursor(node["comments"]["pageInfo"], cursor)
    return comments


def fetch_review_threads(repo: str, number: int, cwd: Path | str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (pull request metadata, every review thread with every comment).

    Both the thread connection and each thread's nested comment connection are paged
    to completion, so fingerprints cover all comments.
    """
    owner, name = repo.split("/", 1)
    threads: list[dict[str, Any]] = []
    meta: dict[str, Any] | None = None
    cursor: str | None = None
    while True:
        variables: dict[str, str | int] = {
            "owner": owner,
            "name": name,
            "number": number,
            "threadsFirst": THREADS_PAGE_SIZE,
            "commentsFirst": COMMENTS_PAGE_SIZE,
        }
        if cursor:
            variables["cursor"] = cursor
        pull = _pull_request(graphql(THREADS_QUERY, variables, cwd), repo, number)
        page = pull.pop("reviewThreads")
        meta = meta or pull
        for node in page["nodes"]:
            if not node:
                continue
            comments = list(c for c in node["comments"]["nodes"] if c)
            more = _next_cursor(node["comments"]["pageInfo"], None)
            if more:
                comments.extend(_remaining_comments(node["id"], more, cwd))
            threads.append({**node, "comments": comments})
        cursor = _next_cursor(page["pageInfo"], cursor)
        if cursor is None:
            return meta, threads
