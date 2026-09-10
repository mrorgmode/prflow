"""Spike D: deterministic review-thread GraphQL via `gh api graphql`.

Read path (safe, any PR you can read):

    uv run python spikes/spike_d_graphql.py --repo OWNER/REPO --pr N

Write path (staged GitHub mutations; **never** run without an explicit test PR):

    PRFLOW_ALLOW_GITHUB_WRITE=1 uv run python spikes/spike_d_graphql.py --repo OWNER/REPO --pr N \
        --reply-thread PRRT_... --body "text"          # addPullRequestReviewThreadReply
    PRFLOW_ALLOW_GITHUB_WRITE=1 uv run python spikes/spike_d_graphql.py --repo OWNER/REPO --pr N \
        --resolve-thread PRRT_...                       # resolveReviewThread

Without the environment flag the mutation argv is printed and nothing is sent.
Everything goes through the `gh` binary; there is no HTTP client here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from typing import Any

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      number
      headRefOid
      headRefName
      baseRefName
      isDraft
      url
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          isOutdated
          isCollapsed
          path
          line
          originalLine
          startLine
          diffSide
          comments(first: 50) {
            nodes {
              id
              databaseId
              url
              author { login }
              body
              createdAt
              updatedAt
              path
              line
              originalLine
              outdated
            }
          }
        }
      }
    }
  }
}
"""

REPLY_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id databaseId url createdAt }
  }
}
"""

RESOLVE_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""


def gh_graphql_argv(query: str, variables: dict[str, Any]) -> list[str]:
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) and not isinstance(value, bool) else "-f"
        argv.extend([flag, f"{key}={value}"])
    return argv


def run_gh(argv: list[str]) -> dict[str, Any]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"gh failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout)


def fetch_review_threads(repo: str, pr: int) -> dict[str, Any]:
    owner, name = repo.split("/", 1)
    threads: list[dict[str, Any]] = []
    cursor: str | None = None
    pull: dict[str, Any] = {}
    while True:
        variables: dict[str, Any] = {"owner": owner, "name": name, "number": pr}
        if cursor:
            variables["cursor"] = cursor
        data = run_gh(gh_graphql_argv(THREADS_QUERY, variables))
        pull = data["data"]["repository"]["pullRequest"]
        page = pull["reviewThreads"]
        threads.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    pull = dict(pull)
    pull.pop("reviewThreads", None)
    return {"pull_request": pull, "threads": threads}


def normalize_thread(node: dict[str, Any]) -> dict[str, Any]:
    """Shape a GraphQL node into the spec 10.2 review-thread record (alias assigned by prflow later)."""
    comments = [
        {
            "id": c["id"],
            "database_id": c.get("databaseId"),
            "author": (c.get("author") or {}).get("login"),
            "body": c["body"],
            "created_at": c["createdAt"],
            "updated_at": c["updatedAt"],
            "url": c.get("url"),
        }
        for c in node["comments"]["nodes"]
    ]
    record = {
        "github_thread_id": node["id"],
        "resolved": node["isResolved"],
        "outdated": node["isOutdated"],
        "path": node.get("path"),
        "line": node.get("line"),
        "original_line": node.get("originalLine"),
        "comments": comments,
    }
    record["source_fingerprint"] = source_fingerprint(record)
    return record


def source_fingerprint(record: dict[str, Any]) -> str:
    """Spec 10.2: sha256 of canonical JSON of exactly these fields."""
    canonical = {
        "github_thread_id": record["github_thread_id"],
        "isResolved": record["resolved"],
        "isOutdated": record["outdated"],
        "comments": sorted(((c["id"], c["updated_at"]) for c in record["comments"]), key=lambda pair: pair[0]),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def reply_argv(thread_id: str, body: str) -> list[str]:
    return gh_graphql_argv(REPLY_MUTATION, {"threadId": thread_id, "body": body})


def resolve_argv(thread_id: str) -> list[str]:
    return gh_graphql_argv(RESOLVE_MUTATION, {"threadId": thread_id})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--reply-thread")
    parser.add_argument("--body")
    parser.add_argument("--resolve-thread")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    snapshot = fetch_review_threads(args.repo, args.pr)
    records = [normalize_thread(n) for n in snapshot["threads"]]
    unresolved = [r for r in records if not r["resolved"]]
    if args.json:
        print(json.dumps({"pull_request": snapshot["pull_request"], "threads": records}, indent=2))
    else:
        pr = snapshot["pull_request"]
        print(f"PR #{pr['number']} {pr['headRefName']} -> {pr['baseRefName']} head={pr['headRefOid'][:12]} draft={pr['isDraft']}")
        print(f"threads: {len(records)} total, {len(unresolved)} unresolved")
        for i, r in enumerate(unresolved, 1):
            first = r["comments"][0] if r["comments"] else {}
            print(f"  T{i} {r['github_thread_id']} {r['path']}:{r['line']} outdated={r['outdated']} comments={len(r['comments'])} fp={r['source_fingerprint'][:19]}")
            print(f"     {first.get('author')}: {str(first.get('body', ''))[:100]!r}")

    allow_write = os.environ.get("PRFLOW_ALLOW_GITHUB_WRITE") == "1"
    for label, argv in (
        ("reply", reply_argv(args.reply_thread, args.body) if args.reply_thread and args.body else None),
        ("resolve", resolve_argv(args.resolve_thread) if args.resolve_thread else None),
    ):
        if argv is None:
            continue
        if not allow_write:
            print(f"[dry-run] {label} mutation NOT sent (set PRFLOW_ALLOW_GITHUB_WRITE=1): {argv[:3]} ... threadId={argv[-1]}", file=sys.stderr)
            continue
        print(json.dumps({label: run_gh(argv)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
