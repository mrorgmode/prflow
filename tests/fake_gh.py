"""Fake `gh` executable for offline Phase 1 tests (conftest puts a wrapper on PATH).

Behaviour comes from the JSON scenario file named by $PRFLOW_FAKE_GH_SCENARIO, which
tests may rewrite between invocations. Every argv is appended to $PRFLOW_FAKE_GH_LOG.
Only the read calls prflow makes are implemented; GraphQL mutations are refused.
Cursors are stringified offsets so pagination of threads and nested comments is real.
"""

from __future__ import annotations

import json
import os
import sys

PR_KEYS = ("id", "number", "url", "title", "state", "isDraft", "headRefName", "baseRefName", "headRefOid", "isCrossRepository")


def emit(obj: object) -> int:
    sys.stdout.write(json.dumps(obj))
    return 0


def fail(message: str, code: int = 1) -> int:
    sys.stderr.write(message + "\n")
    return code


def options(args: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--") and "=" in arg:
            key, value = arg.split("=", 1)
            found[key] = value
        elif arg.startswith("--") and i + 1 < len(args):
            found[arg] = args[i + 1]
            i += 1
        i += 1
    return found


def graphql_args(args: list[str]) -> tuple[str, dict[str, object]]:
    query, variables = "", {}
    for flag, value in zip(args, args[1:]):
        if flag in ("-f", "-F"):
            key, raw = value.split("=", 1)
            if key == "query":
                query = raw
            else:
                variables[key] = int(raw) if flag == "-F" and raw.isdigit() else raw
    return query, variables


def comments_page(comments: list[dict], first: int, cursor: object) -> dict:
    start = int(cursor) if cursor else 0
    nodes = comments[start : start + first]
    end = start + len(nodes)
    return {"pageInfo": {"hasNextPage": end < len(comments), "endCursor": str(end) if nodes else None}, "nodes": nodes}


def main(argv: list[str]) -> int:
    with open(os.environ["PRFLOW_FAKE_GH_SCENARIO"]) as handle:
        scenario = json.load(handle)
    with open(os.environ["PRFLOW_FAKE_GH_LOG"], "a") as log:
        log.write(json.dumps(argv) + "\n")

    key = " ".join(argv[:2])
    if key in scenario.get("fail", {}):
        spec = scenario["fail"][key]
        return fail(spec["stderr"], spec.get("code", 1))
    if argv[:1] == ["--version"]:
        print("gh version 2.100.0 (fake)")
        return 0
    if key == "auth status":
        if not scenario.get("authenticated", True):
            return fail("You are not logged into any GitHub hosts. To log in, run: gh auth login")
        account = {"state": "success", "active": True, "host": "github.com", "login": scenario.get("login", "octo")}
        return emit({"hosts": {"github.com": [account]}})
    if key == "repo view":
        rest = argv[2:]
        positional = [a for i, a in enumerate(rest) if not a.startswith("-") and (i == 0 or rest[i - 1] != "--json")]
        name = positional[0] if positional else scenario.get("repo")
        if name is None:
            return fail("none of the git remotes configured for this repository point to a known GitHub host.")
        if positional and name.lower() not in {r.lower() for r in scenario.get("repos", [scenario.get("repo")])}:
            return fail(f"GraphQL: Could not resolve to a Repository with the name '{name}'. (repository)")
        return emit({"nameWithOwner": name, "url": f"https://github.com/{name}"})
    if key == "pr list":
        opts = options(argv[2:])
        prs = [p for p in scenario["prs"] if p.get("repo", scenario.get("repo")) == opts.get("--repo") and p["state"] == "OPEN"]
        if "--head" in opts:
            prs = [p for p in prs if p["headRefName"] == opts["--head"]]
        fields = opts["--json"].split(",")
        rows = []
        for p in prs:
            row = {**p, "headRepositoryOwner": {"login": p.get("headOwner", "acme")}}
            rows.append({f: row.get(f) for f in fields})
        return emit(rows)
    if key == "api graphql":
        query, variables = graphql_args(argv[2:])
        if query.lstrip().startswith("mutation") or "mutation" in query:
            return fail("fake gh refuses GraphQL mutations", 3)
        if "node(id:" in query:
            for pr in scenario["prs"]:
                for thread in pr.get("threads", []):
                    if thread["id"] == variables["id"]:
                        page = comments_page(thread["comments"], int(variables["commentsFirst"]), variables.get("cursor"))
                        return emit({"data": {"node": {"comments": page}}})
            return emit({"data": {"node": None}})
        repo = f"{variables['owner']}/{variables['name']}"
        pr = next(
            (p for p in scenario["prs"] if p.get("repo", scenario.get("repo")) == repo and p["number"] == variables["number"]),
            None,
        )
        if pr is None:
            return fail(
                f"GraphQL: Could not resolve to a PullRequest with the number of {variables['number']}. (repository.pullRequest)"
            )
        meta = {k: pr.get(k) for k in PR_KEYS}
        if "reviewThreads" in query:
            threads = pr.get("threads", [])
            start = int(variables["cursor"]) if variables.get("cursor") else 0
            chunk = threads[start : start + int(variables["threadsFirst"])]
            end = start + len(chunk)
            nodes = [
                {**t, "comments": comments_page(t["comments"], int(variables["commentsFirst"]), None)} for t in chunk
            ]
            meta["reviewThreads"] = {
                "pageInfo": {"hasNextPage": end < len(threads), "endCursor": str(end) if chunk else None},
                "nodes": nodes,
            }
        return emit({"data": {"repository": {"pullRequest": meta}}})
    return fail(f"fake gh: unsupported invocation {argv}", 64)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
