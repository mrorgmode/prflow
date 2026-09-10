"""Builders for fake GitHub review data (GraphQL node shapes) used by Phase 1 tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

REPO = "acme/widgets"
HUNK = "@@ -1,2 +1,2 @@\n-print('hello')\n+print('hi')"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def comment(cid: str, body: str = "Please fix this.", *, author: str | None = "reviewer", created: str = "2026-09-01T10:00:00Z", updated: str | None = None) -> dict[str, Any]:
    return {
        "id": cid,
        "databaseId": sum(map(ord, cid)),
        "url": f"https://github.com/{REPO}/pull/7#discussion_{cid}",
        "author": {"login": author} if author else None,
        "body": body,
        "createdAt": created,
        "updatedAt": updated or created,
        "diffHunk": HUNK,
    }


def thread(tid: str, *comments: dict[str, Any], resolved: bool = False, outdated: bool = False, **fields: Any) -> dict[str, Any]:
    node = {
        "id": tid,
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "app.py",
        "line": 1,
        "originalLine": 1,
        "startLine": None,
        "originalStartLine": None,
        "diffSide": "RIGHT",
        "subjectType": "LINE",
        "resolvedBy": {"login": "reviewer"} if resolved else None,
        "comments": list(comments),
    }
    node.update(fields)
    return node


def pull_request(number: int, head: str, threads: list[dict[str, Any]] | None = None, **fields: Any) -> dict[str, Any]:
    pr = {
        "id": f"PR_{number}",
        "number": number,
        "url": f"https://github.com/{REPO}/pull/{number}",
        "title": f"Change {number}",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": head,
        "baseRefName": "main",
        "headRefOid": f"{number:040x}",
        "isCrossRepository": False,
        "threads": threads or [],
    }
    pr.update(fields)
    return pr
