"""`prflow` command line: argparse, rendering and exit codes only.

With --json, stdout carries exactly one JSON document, for failures too:
``{"ok": false, "command": ..., "error": {"code", "message", "hint"?, "details"?}}``.
Diagnostics go to stderr. Exit codes: 0 ok, 1 error (or doctor: Phase 1 not ready),
2 usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from . import __version__, doctor, review
from .errors import PrflowError
from .git import inspect_checkout

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(value: Any) -> str:
    """Neutralize terminal control sequences in (untrusted) GitHub text for human output."""
    return _CONTROL_RE.sub(lambda m: f"\\x{ord(m.group()):02x}", str(value))


def _short(sha: str | None) -> str:
    return (sha or "-")[:12]


def _location(t: dict[str, Any]) -> str:
    path = clean(t["path"] or "(no file)")
    if t["line"] is not None:
        return f"{path}:{t['line']}"
    if t["original_line"] is not None:
        return f"{path}:{t['original_line']} (original)"
    return path


def _pr_header(pr: dict[str, Any]) -> str:
    return f"PR #{pr['pr_number']} {pr['repo']}  {clean(pr['branch'])} -> {clean(pr['base_branch'])}"


# --- rendering ---------------------------------------------------------------------


def render_status(p: dict[str, Any]) -> str:
    co, pr = p["checkout"], p["pr"]
    flags = [pr["state"] or "?"] + (["draft"] if pr["is_draft"] else [])
    via = "current branch" if p["resolved_by"] == "branch" else "--pr"
    lines = [
        f"repo     {pr['repo']}",
        f"checkout {co['root']}",
        f"branch   {clean(co['branch'] or '(detached HEAD)')}  HEAD {_short(co['head'])}"
        + ("  (uncommitted changes)" if co["dirty"] else ""),
        f"PR #{pr['pr_number']}".ljust(9) + f"{clean(pr['title'])}  [{', '.join(flags)}]  (via {via})",
        f"         {clean(pr['branch'])} -> {clean(pr['base_branch'])}  head {_short(pr['head_sha'])} "
        + ("(= local HEAD)" if p["head_matches_local"] else "(differs from local HEAD)"),
        f"         {pr['url']}",
    ]
    if pr["stack_parent_pr"] or pr["stack_child_prs"]:
        parent = f"#{pr['stack_parent_pr']}" if pr["stack_parent_pr"] else "none"
        children = ", ".join(f"#{n}" for n in pr["stack_child_prs"]) or "none"
        lines.append(f"stack    parent {parent}; children {children}")
    reviews = p["reviews"]
    if reviews is None:
        lines.append("reviews  not fetched yet; run `prflow refresh`")
    else:
        lines.append(f"reviews  {reviews['unresolved']} unresolved of {reviews['total']} (cached {reviews['refreshed_at']}); `prflow review list`")
    lines += [f"note     {n}" for n in p["notes"]]
    lines.append(f"state    {p['state']['path']} (revision {p['state']['revision']})")
    return "\n".join(lines)


def render_refresh(p: dict[str, Any]) -> str:
    s = p["summary"]
    if s["superseded"]:
        return f"{_pr_header(p['pr'])}: a concurrent refresh stored a newer snapshot; nothing applied."
    lines = [f"Refreshed {_pr_header(p['pr'])}: {p['unresolved']} unresolved of {p['total']} review threads (state revision {p['state_revision']})"]
    for label, key in (("new", "new"), ("changed", "changed"), ("reopened", "reopened"), ("no longer on GitHub", "missing")):
        if s[key]:
            lines.append(f"  {label}: {', '.join(s[key])}")
    if s["resolved"]:
        lines.append("  resolved on GitHub: " + ", ".join(f"{r['thread']} ({r['state'] or 'untracked'})" for r in s["resolved"]))
    if s["rebound_from"]:
        lines.append(f"  this branch previously pointed at {s['rebound_from']}")
    return "\n".join(lines)


def render_list(p: dict[str, Any]) -> str:
    lines = [f"{_pr_header(p['pr'])}  (cached {p['refreshed_at']}, state revision {p['state_revision']})"]
    if not p["threads"]:
        lines.append("No review threads." if p["include_all"] else "No unresolved review threads.")
    for t in p["threads"]:
        tags = [t["state"]] if t["state"] else []
        tags += [name for name, on in (("resolved", t["resolved"]), ("outdated", t["outdated"])) if on]
        if not t["present_on_github"]:
            tags.append("gone from GitHub")
        count = f"{t['comment_count']} comment{'s' if t['comment_count'] != 1 else ''}"
        lines.append(f"{t['alias'] or '-':<4} {_location(t)}  [{', '.join(tags)}]  {count}")
        lines.append(f"     {clean(t['first_author'] or '(deleted user)')}: {clean(t['excerpt'])}")
    lines.append(f"{p['unresolved']} unresolved. Details: `prflow review show T<n>`; update: `prflow refresh`.")
    return "\n".join(lines)


def render_show(p: dict[str, Any]) -> str:
    t = p["thread"]
    status = "resolved" + (f" by {clean(t['resolved_by'])}" if t["resolved_by"] else "") if t["resolved"] else "unresolved"
    lines = [
        f"{t['alias'] or '(no alias)'}  {_location(t)}  {_pr_header(p['pr'])}",
        f"thread   {t['github_thread_id']}  {status}{', outdated' if t['outdated'] else ''}"
        + ("" if t.get("present_on_github", True) else ", gone from GitHub"),
        f"state    {t['state'] or '-'}" + (f" ({t['state_reason']})" if t["state_reason"] else ""),
        f"source   {t['source_fingerprint']}",
        f"cached   {p['refreshed_at']} (state revision {p['state_revision']})",
    ]
    if t.get("diff_hunk"):
        hunk = t["diff_hunk"].splitlines()[-8:]
        lines.append("diff")
        lines += [f"  | {clean(h)}" for h in hunk]
    for i, c in enumerate(t["comments"], 1):
        edited = f" (edited {c['updated_at']})" if c["updated_at"] != c["created_at"] else ""
        lines.append("")
        lines.append(f"[{i}] {clean(c['author'] or '(deleted user)')}  {c['created_at']}{edited}")
        lines += [f"    {clean(line)}" for line in (c["body"] or "").splitlines() or [""]]
        if c["url"]:
            lines.append(f"    {c['url']}")
    return "\n".join(lines)


def render_doctor(p: dict[str, Any]) -> str:
    s = p["summary"]
    lines = [
        f"Phase 1 read-only orientation: {'READY' if s['phase1_ready'] else 'NOT READY'}",
        f"interactive Codex: {s['interactive']}",
        f"batch triage/fix (Phase 2): {s['batch'].upper()}",
    ]
    group = None
    for c in p["checks"]:
        if c["group"] != group:
            group = c["group"]
            lines.append(f"\n[{group}]")
        lines.append(f"  {c['status']:<11} {c['name']:<16} {clean(c['detail'])}")
        if c["hint"] and c["status"] not in ("ok", "info"):
            lines.append(f"  {'':<11} {'':<16} hint: {clean(c['hint'])}")
    return "\n".join(lines)


# --- commands ----------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> tuple[dict[str, Any], str, int]:
    report = doctor.run(Path.cwd(), args.repo, args.pr)
    return report, render_doctor(report), EXIT_OK if report["summary"]["phase1_ready"] else EXIT_ERROR


def cmd_status(args: argparse.Namespace) -> tuple[dict[str, Any], str, int]:
    payload = review.status(inspect_checkout(Path.cwd()), args.repo, args.pr)
    return payload, render_status(payload), EXIT_OK


def cmd_refresh(args: argparse.Namespace) -> tuple[dict[str, Any], str, int]:
    payload = review.refresh(inspect_checkout(Path.cwd()), args.repo, args.pr, args.expected_revision)
    return payload, render_refresh(payload), EXIT_OK


def cmd_review_list(args: argparse.Namespace) -> tuple[dict[str, Any], str, int]:
    checkout = inspect_checkout(Path.cwd())
    payload = review.review_list(checkout, args.repo, args.pr, include_all=args.all, refresh_first=args.refresh)
    return payload, render_list(payload), EXIT_OK


def cmd_review_show(args: argparse.Namespace) -> tuple[dict[str, Any], str, int]:
    checkout = inspect_checkout(Path.cwd())
    payload = review.review_show(checkout, args.thread, args.repo, args.pr, refresh_first=args.refresh)
    return payload, render_show(payload), EXIT_OK


# --- parsing -----------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise PrflowError("usage", message, hint=f"see `{self.prog} --help`")


def _pr_number(text: str) -> int:
    if not text.isdigit() or int(text) <= 0:
        raise argparse.ArgumentTypeError(f"not a pull request number: {text!r}")
    return int(text)


def build_parser() -> argparse.ArgumentParser:
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--json", action="store_true", help="write one JSON document to stdout (errors too); diagnostics go to stderr")
    target = argparse.ArgumentParser(add_help=False)
    target.add_argument("--repo", metavar="OWNER/REPO", help="GitHub repository (default: resolved by gh from this checkout)")
    target.add_argument("--pr", type=_pr_number, metavar="N", help="pull request number (default: the open PR for the current branch)")
    cached = argparse.ArgumentParser(add_help=False)
    cached.add_argument("--refresh", action="store_true", help="fetch from GitHub first (default: read the local cache only)")

    parser = _Parser(
        prog="prflow",
        description="Read-only pull-request orientation and review-thread inspection (Phase 1). Makes no GitHub changes.",
    )
    parser.add_argument("--version", action="version", version=f"prflow {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    p = commands.add_parser("doctor", parents=[output, target], help="check the environment: Phase 1 readiness vs. batch readiness")
    p.set_defaults(handler=cmd_doctor)
    p = commands.add_parser("status", parents=[output, target], help="show checkout, PR and cached review summary (network read)")
    p.set_defaults(handler=cmd_status)
    p = commands.add_parser("refresh", parents=[output, target], help="fetch all review threads from GitHub into local state")
    p.add_argument("--expected-revision", type=int, metavar="N", help="refuse to write unless local state is still at revision N")
    p.set_defaults(handler=cmd_refresh)

    reviews = commands.add_parser("review", help="inspect review threads").add_subparsers(
        dest="review_command", metavar="SUBCOMMAND", required=True
    )
    p = reviews.add_parser("list", parents=[output, target, cached], help="list cached review threads (unresolved by default)")
    p.add_argument("--all", action="store_true", help="include resolved threads and threads gone from GitHub")
    p.set_defaults(handler=cmd_review_list)
    p = reviews.add_parser("show", parents=[output, target, cached], help="show one cached review thread with all comments")
    p.add_argument("thread", metavar="THREAD", help="stable alias such as T3, or a GitHub review-thread node ID")
    p.set_defaults(handler=cmd_review_show)
    return parser


def _command_name(argv: list[str]) -> str | None:
    words = [a for a in argv if not a.startswith("-")]
    if not words:
        return None
    return " ".join(words[:2]) if words[0] == "review" and len(words) > 1 else words[0]


def _fail(exc: PrflowError, command: str | None, as_json: bool, code: int | None = None) -> int:
    if as_json:
        print(json.dumps({"ok": False, "command": command, "error": exc.to_json()}, indent=2, ensure_ascii=False))
    else:
        print(f"prflow: error: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
    if code is not None:
        return code
    return EXIT_USAGE if exc.code == "usage" else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    command = _command_name(argv)
    try:
        args = build_parser().parse_args(argv)
        command = args.command + (f" {args.review_command}" if getattr(args, "review_command", None) else "")
        payload, text, code = args.handler(args)
    except PrflowError as exc:
        return _fail(exc, command, as_json)
    except KeyboardInterrupt:
        return _fail(PrflowError("interrupted", "interrupted"), command, as_json, code=130)
    except Exception as exc:  # keep the JSON contract even for bugs; traceback goes to stderr
        traceback.print_exc(file=sys.stderr)
        return _fail(PrflowError("internal", f"unexpected {type(exc).__name__}: {exc}"), command, as_json)
    if as_json:
        print(json.dumps({"ok": code == EXIT_OK, "command": command, **payload}, indent=2, ensure_ascii=False))
    else:
        print(text)
    return code
