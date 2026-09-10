"""Review-thread orchestration: PR resolution policy, snapshot merge, stable aliases.

No subprocesses here; GitHub and state I/O go through github.py and state.py.

State scoping: cached PRs are keyed by "OWNER/REPO#N" and each keeps its own alias
counter, so switching the checkout to another PR never mixes threads or aliases.
A branch is bound to a PR only by a `refresh` that resolved the PR from that branch.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from . import github
from . import state as state_store
from .errors import PrflowError
from .git import Checkout

PR_CONTEXT_KEYS = (
    "repo",
    "pr_number",
    "url",
    "title",
    "state",
    "branch",
    "base_branch",
    "head_sha",
    "is_draft",
    "is_cross_repository",
)
ALIAS_RE = re.compile(r"^[Tt](\d+)$")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def pr_key(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def source_fingerprint(thread_id: str, resolved: bool, outdated: bool, comments: list[dict[str, Any]]) -> str:
    """SPEC §10.2: SHA-256 of canonical JSON of exactly these four fields."""
    canonical = {
        "github_thread_id": thread_id,
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": sorted(([c["id"], c["updated_at"]] for c in comments), key=lambda pair: pair[0]),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def normalize_thread(node: dict[str, Any]) -> dict[str, Any]:
    """Shape a GraphQL review-thread node (comments fully paged) into the stored record."""
    comments = [
        {
            "id": c["id"],
            "database_id": c.get("databaseId"),
            "url": c.get("url"),
            "author": (c.get("author") or {}).get("login"),  # None for deleted accounts
            "body": c.get("body") or "",
            "created_at": c.get("createdAt"),
            "updated_at": c.get("updatedAt"),
        }
        for c in node["comments"]
    ]
    resolved = bool(node["isResolved"])
    outdated = bool(node["isOutdated"])
    return {
        "github_thread_id": node["id"],
        "resolved": resolved,
        "outdated": outdated,
        "resolved_by": (node.get("resolvedBy") or {}).get("login"),
        "path": node.get("path"),
        "line": node.get("line"),
        "original_line": node.get("originalLine"),
        "start_line": node.get("startLine"),
        "original_start_line": node.get("originalStartLine"),
        "diff_side": node.get("diffSide"),
        "subject_type": node.get("subjectType"),
        "diff_hunk": node["comments"][0].get("diffHunk") if node["comments"] else None,
        "comments": comments,
        "source_fingerprint": source_fingerprint(node["id"], resolved, outdated, comments),
    }


def pr_context(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry.get(key) for key in PR_CONTEXT_KEYS}


def _context_from_meta(repo: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": repo,
        "pr_number": meta["number"],
        "node_id": meta.get("id"),
        "url": meta.get("url"),
        "title": meta.get("title"),
        "state": meta.get("state"),
        "branch": meta.get("headRefName"),
        "base_branch": meta.get("baseRefName"),
        "head_sha": meta.get("headRefOid"),
        "is_draft": meta.get("isDraft"),
        "is_cross_repository": meta.get("isCrossRepository"),
    }


# --- which PR ------------------------------------------------------------------


def resolve_target(checkout: Checkout, repo_arg: str | None, pr_arg: int | None) -> tuple[str, int, str]:
    """Return (repo, pr_number, how) where how is "explicit" or "branch".

    Without --pr, only an unambiguous open same-repository PR whose head is the
    current branch (or its configured upstream branch name) is accepted.
    """
    repo = github.resolve_repo(checkout.root, repo_arg)
    if pr_arg is not None:
        return repo, pr_arg, "explicit"
    if checkout.branch is None:
        raise PrflowError(
            "detached_head",
            "HEAD is detached, so there is no current branch to match to a pull request",
            hint="check out the PR branch, or pass --pr N",
        )
    head = checkout.upstream_branch or checkout.branch
    candidates = github.open_prs_for_head(repo, head, checkout.root)
    brief = [
        {
            "number": c["number"],
            "url": c.get("url"),
            "base_branch": c.get("baseRefName"),
            "head_owner": (c.get("headRepositoryOwner") or {}).get("login"),
            "is_cross_repository": c.get("isCrossRepository"),
        }
        for c in candidates
    ]
    same_repo = [c for c in candidates if not c.get("isCrossRepository")]
    if len(same_repo) == 1:
        return repo, same_repo[0]["number"], "branch"
    if same_repo:
        raise PrflowError(
            "pr_ambiguous",
            f"{len(same_repo)} open pull requests in {repo} have head branch {head!r}",
            hint="pass --pr N to choose one",
            details={"repo": repo, "head_branch": head, "candidates": brief},
        )
    if candidates:
        raise PrflowError(
            "pr_ambiguous",
            f"only pull requests from forks use head branch {head!r} in {repo}; prflow will not guess",
            hint="pass --pr N if one of them is yours",
            details={"repo": repo, "head_branch": head, "candidates": brief},
        )
    raise PrflowError(
        "no_pr",
        f"no open pull request in {repo} has head branch {head!r}",
        hint="push the branch and open a PR, or pass --pr N",
        details={"repo": repo, "head_branch": head},
    )


# --- merging a snapshot into state ------------------------------------------------


def _allocate_alias(entry: dict[str, Any]) -> str:
    alias = f"T{entry['next_alias']}"
    entry["next_alias"] += 1
    return alias


def _label(record: dict[str, Any]) -> str:
    return record.get("alias") or record["github_thread_id"]


def merge_snapshot(
    st: dict[str, Any],
    context: dict[str, Any],
    threads: list[dict[str, Any]],
    fetched_at: str,
    bind_branch: str | None = None,
) -> dict[str, Any]:
    """Merge one complete thread snapshot into the latest state (called under the lock).

    Aliases are allocated when a thread is first seen unresolved and never reused.
    Refresh semantics follow SPEC §11.6.
    """
    key = pr_key(context["repo"], context["pr_number"])
    entry = st["prs"].setdefault(key, {"next_alias": 1, "threads": {}, "fetched_at": None})
    summary: dict[str, Any] = {
        "pr": key,
        "superseded": False,
        "new": [],
        "changed": [],
        "resolved": [],
        "reopened": [],
        "missing": [],
        "rebound_from": None,
    }
    if entry["fetched_at"] and entry["fetched_at"] > fetched_at:
        # A concurrent refresh that started later already stored a newer snapshot.
        summary["superseded"] = True
        return summary
    entry.update(context)
    entry["fetched_at"] = fetched_at
    known: dict[str, dict[str, Any]] = entry["threads"]

    def first_created(record: dict[str, Any]) -> tuple[str, str]:
        created = record["comments"][0]["created_at"] if record["comments"] else ""
        return (created or "", record["github_thread_id"])

    seen = set()
    for fresh in sorted(threads, key=first_created):
        tid = fresh["github_thread_id"]
        seen.add(tid)
        record = known.get(tid)
        if record is None:
            record = {**fresh, "alias": None, "state": None, "state_reason": None, "first_seen_at": fetched_at}
            record["present_on_github"] = True
            if not fresh["resolved"]:
                record["alias"] = _allocate_alias(entry)
                record["state"] = "discovered"
                summary["new"].append(record["alias"])
            known[tid] = record
            continue

        was_resolved = record["resolved"]
        if record["source_fingerprint"] != fresh["source_fingerprint"]:
            record["fingerprint_changed_at"] = fetched_at
            summary["changed"].append(_label(record))
        record.update(fresh)
        record["present_on_github"] = True

        if record["alias"] is None and not fresh["resolved"]:
            # Known only as resolved until now: it is a work item from here on.
            record["alias"] = _allocate_alias(entry)
            record["state"] = "discovered"
            summary["reopened"].append(record["alias"])
        elif was_resolved and not fresh["resolved"]:
            summary["reopened"].append(_label(record))
            if record["state"] == "done":
                record["state"], record["state_reason"] = "discovered", None
            # A "stale" item keeps its preserved local state for human reconciliation.
        elif not was_resolved and fresh["resolved"]:
            if record["state"] in ("discovered", "triaged"):
                record["state"], record["state_reason"] = "done", "resolved_on_github"
            elif record["state"] not in (None, "done", "stale"):
                # Local work in progress: never discard it, flag it for the human.
                record["state"], record["state_reason"] = "stale", "resolved_on_github"
            summary["resolved"].append({"thread": _label(record), "state": record["state"]})

    for tid, record in known.items():
        if tid not in seen and record.get("present_on_github", True):
            record["present_on_github"] = False
            summary["missing"].append(_label(record))

    if bind_branch:
        previous = st["branches"].get(bind_branch)
        if previous is None or previous.get("pr") != key:
            summary["rebound_from"] = previous.get("pr") if previous else None
            st["branches"][bind_branch] = {"pr": key, "bound_at": fetched_at}
    return summary


def counts(entry: dict[str, Any]) -> dict[str, int]:
    present = [t for t in entry["threads"].values() if t.get("present_on_github", True)]
    return {"unresolved": sum(1 for t in present if not t["resolved"]), "total": len(present)}


# --- commands ---------------------------------------------------------------------


def refresh(
    checkout: Checkout, repo_arg: str | None = None, pr_arg: int | None = None, expected_revision: int | None = None
) -> dict[str, Any]:
    repo, number, how = resolve_target(checkout, repo_arg, pr_arg)
    fetched_at = utc_now()
    # Network reads happen outside the lock; the merge rereads the latest state under it.
    meta, nodes = github.fetch_review_threads(repo, number, checkout.root)
    context = _context_from_meta(repo, meta)
    threads = [normalize_thread(node) for node in nodes]
    with state_store.transaction(checkout.state_dir, expected_revision) as st:
        summary = merge_snapshot(st, context, threads, fetched_at, checkout.branch if how == "branch" else None)
        entry = st["prs"][summary["pr"]]
    return {
        "pr": pr_context(entry),
        "resolved_by": how,
        "refreshed_at": entry["fetched_at"],
        "state_revision": st["state_revision"],
        **counts(entry),
        "summary": summary,
    }


def cached_entry(st: dict[str, Any], checkout: Checkout, repo_arg: str | None, pr_arg: int | None) -> dict[str, Any]:
    """Find the cached PR for this invocation without touching the network."""
    if pr_arg is not None:
        matches = [
            e
            for e in st["prs"].values()
            if e.get("pr_number") == pr_arg and (repo_arg is None or str(e.get("repo")).lower() == repo_arg.lower())
        ]
        if len(matches) > 1:
            raise PrflowError("pr_ambiguous", f"cached state has PR #{pr_arg} for several repositories", hint="pass --repo OWNER/REPO")
        if not matches:
            raise PrflowError("not_refreshed", f"no cached review threads for PR #{pr_arg}", hint=f"run `prflow refresh --pr {pr_arg}`")
        return matches[0]
    if checkout.branch is None:
        raise PrflowError("detached_head", "HEAD is detached, so there is no current branch to look up", hint="pass --pr N")
    binding = st["branches"].get(checkout.branch)
    entry = st["prs"].get(binding["pr"]) if binding else None
    if entry is None or (repo_arg is not None and str(entry.get("repo")).lower() != repo_arg.lower()):
        raise PrflowError(
            "not_refreshed",
            f"no cached pull request for branch {checkout.branch!r} in this checkout",
            hint="run `prflow refresh` (or use --refresh)",
        )
    return entry


def _load(checkout: Checkout, repo_arg: str | None, pr_arg: int | None, refresh_first: bool) -> tuple[dict[str, Any], int]:
    if refresh_first:
        result = refresh(checkout, repo_arg, pr_arg)
        st = state_store.read_state(checkout.state_dir)
        return st["prs"][result["summary"]["pr"]], st["state_revision"]
    st = state_store.read_state(checkout.state_dir)
    return cached_entry(st, checkout, repo_arg, pr_arg), st["state_revision"]


def _alias_order(record: dict[str, Any]) -> tuple[int, int, str]:
    match = ALIAS_RE.match(record.get("alias") or "")
    return (0, int(match.group(1)), "") if match else (1, 0, record.get("first_seen_at") or "")


def thread_summary(record: dict[str, Any]) -> dict[str, Any]:
    comments = record["comments"]
    first = comments[0] if comments else {}
    body = " ".join(str(first.get("body", "")).split())
    return {
        "alias": record["alias"],
        "github_thread_id": record["github_thread_id"],
        "state": record["state"],
        "state_reason": record["state_reason"],
        "resolved": record["resolved"],
        "outdated": record["outdated"],
        "present_on_github": record.get("present_on_github", True),
        "path": record["path"],
        "line": record["line"],
        "original_line": record["original_line"],
        "comment_count": len(comments),
        "first_author": first.get("author"),
        "excerpt": body[:120] + ("…" if len(body) > 120 else ""),
        "last_updated_at": max((c["updated_at"] or "" for c in comments), default=None),
        "url": first.get("url"),
        "source_fingerprint": record["source_fingerprint"],
    }


def _cached_payload(entry: dict[str, Any], revision: int) -> dict[str, Any]:
    return {"pr": pr_context(entry), "refreshed_at": entry["fetched_at"], "state_revision": revision, **counts(entry)}


def review_list(
    checkout: Checkout, repo_arg: str | None = None, pr_arg: int | None = None, *, include_all: bool = False, refresh_first: bool = False
) -> dict[str, Any]:
    entry, revision = _load(checkout, repo_arg, pr_arg, refresh_first)
    records = sorted(entry["threads"].values(), key=_alias_order)
    if not include_all:
        records = [r for r in records if not r["resolved"] and r.get("present_on_github", True)]
    return {**_cached_payload(entry, revision), "include_all": include_all, "threads": [thread_summary(r) for r in records]}


def find_thread(entry: dict[str, Any], selector: str) -> dict[str, Any]:
    match = ALIAS_RE.match(selector)
    for record in entry["threads"].values():
        if (match and record["alias"] == f"T{int(match.group(1))}") or record["github_thread_id"] == selector:
            return record
    raise PrflowError(
        "unknown_thread",
        f"no review thread {selector!r} in cached PR #{entry['pr_number']}",
        hint="see `prflow review list --all`, or `prflow refresh`",
    )


def review_show(
    checkout: Checkout, selector: str, repo_arg: str | None = None, pr_arg: int | None = None, *, refresh_first: bool = False
) -> dict[str, Any]:
    entry, revision = _load(checkout, repo_arg, pr_arg, refresh_first)
    record = find_thread(entry, selector)
    return {**_cached_payload(entry, revision), "thread": dict(record)}


def _stack(repo: str, context: dict[str, Any], checkout: Checkout) -> tuple[int | None, list[int]]:
    """Observational stack context (SPEC §21); best effort, never fatal."""
    try:
        prs = github.open_prs(repo, checkout.root)
    except PrflowError:
        return None, []
    number = context["pr_number"]
    parents = [
        p["number"]
        for p in prs
        if p["number"] != number and not p.get("isCrossRepository") and p.get("headRefName") == context["base_branch"]
    ]
    children = sorted(p["number"] for p in prs if p["number"] != number and p.get("baseRefName") == context["branch"])
    return (parents[0] if len(parents) == 1 else None), children


def status(checkout: Checkout, repo_arg: str | None = None, pr_arg: int | None = None) -> dict[str, Any]:
    """Live PR identity plus the cached review summary. Reads state; never writes it."""
    repo, number, how = resolve_target(checkout, repo_arg, pr_arg)
    context = _context_from_meta(repo, github.fetch_pr(repo, number, checkout.root))
    parent, children = _stack(repo, context, checkout)
    pr = {**pr_context(context), "stack_parent_pr": parent, "stack_child_prs": children}

    st = state_store.read_state(checkout.state_dir)
    key = pr_key(repo, number)
    entry = st["prs"].get(key)
    notes = []
    binding = st["branches"].get(checkout.branch) if checkout.branch else None
    if how == "branch" and binding and binding.get("pr") != key:
        notes.append(f"cached reviews for branch {checkout.branch} belong to {binding['pr']}; `prflow refresh` rebinds it")
    if entry and entry.get("head_sha") != context["head_sha"]:
        notes.append("the PR head moved since the last refresh")
    return {
        "checkout": checkout.to_json(),
        "pr": pr,
        "resolved_by": how,
        "head_matches_local": checkout.head == context["head_sha"],
        "reviews": None if entry is None else {"refreshed_at": entry["fetched_at"], **counts(entry)},
        "state": {"path": str(checkout.state_dir / state_store.STATE_FILE), "revision": st["state_revision"]},
        "notes": notes,
    }
