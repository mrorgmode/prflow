"""End-to-end CLI behaviour with real Git repositories and a fake `gh` on PATH."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from phase1_support import comment, git, pull_request, thread
from prflow import github, review, state
from prflow.errors import PrflowError
from prflow.git import inspect_checkout

SRC = Path(__file__).resolve().parents[1] / "src"
pytestmark = pytest.mark.usefixtures("isolated_env")


def five_comment_scenario(fake_gh) -> None:
    fake_gh.pr(7)["threads"] = [
        thread("PRRT_a", *[comment(f"A{i}", f"note {i}", created=f"2026-09-01T00:00:0{i}Z") for i in range(5)], line=3),
        thread("PRRT_b", comment("B1", created="2026-09-02T00:00:00Z"), resolved=True),
        thread("PRRT_c", comment("C1", created="2026-09-03T00:00:00Z")),
        thread("PRRT_d", comment("D1", author=None, created="2026-09-04T00:00:00Z"), outdated=True, line=None, originalLine=12),
    ]
    fake_gh.save()


def test_refresh_list_show_page_threads_and_nested_comments_completely(repo, fake_gh, run_cli, monkeypatch) -> None:
    monkeypatch.setattr(github, "THREADS_PAGE_SIZE", 2)
    monkeypatch.setattr(github, "COMMENTS_PAGE_SIZE", 2)
    five_comment_scenario(fake_gh)

    r = run_cli(repo, "refresh", "--json")
    assert r.code == 0 and r.data["ok"] is True, r.out
    assert r.data["summary"]["new"] == ["T1", "T2", "T3"]
    assert (r.data["unresolved"], r.data["total"], r.data["state_revision"]) == (3, 4, 1)
    queries = [" ".join(call) for call in fake_gh.calls() if call[:2] == ["api", "graphql"]]
    assert sum("reviewThreads" in q for q in queries) == 2, "two thread pages"
    assert sum("node(id:" in q for q in queries) == 2, "comments 3-4 and 5 of PRRT_a"
    assert not any("mutation" in " ".join(call) for call in fake_gh.calls())

    r = run_cli(repo, "review", "show", "T1", "--json")
    comments = r.data["thread"]["comments"]
    assert [c["id"] for c in comments] == ["A0", "A1", "A2", "A3", "A4"]
    assert r.data["thread"]["source_fingerprint"] == review.source_fingerprint("PRRT_a", False, False, comments)

    r = run_cli(repo, "review", "list")
    assert r.code == 0
    assert "T1   app.py:3" in r.out and "app.py:12 (original)" in r.out and "(deleted user)" in r.out
    assert "PRRT_b" not in r.out and "3 unresolved" in r.out

    r = run_cli(repo, "review", "list", "--all", "--json")
    by_id = {t["github_thread_id"]: t for t in r.data["threads"]}
    assert by_id["PRRT_b"]["alias"] is None and by_id["PRRT_b"]["resolved"] is True
    assert run_cli(repo, "review", "show", "PRRT_b", "--json").data["thread"]["resolved_by"] == "reviewer"


def test_list_and_show_read_the_cache_until_refresh_is_requested(repo, fake_gh, run_cli) -> None:
    r = run_cli(repo, "review", "list", "--json")
    assert r.code == 1 and r.data["error"]["code"] == "not_refreshed" and r.err == ""
    assert fake_gh.calls() == [], "list without --refresh never touches the network"

    fake_gh.pr(7)["threads"] = [thread("PRRT_a", comment("A1"))]
    fake_gh.save()
    assert run_cli(repo, "refresh", "--json").code == 0
    fake_gh.pr(7)["threads"].append(thread("PRRT_b", comment("B1", created="2026-09-09T00:00:00Z")))
    fake_gh.save()

    assert [t["alias"] for t in run_cli(repo, "review", "list", "--json").data["threads"]] == ["T1"]
    r = run_cli(repo, "review", "list", "--refresh", "--json")
    assert [t["alias"] for t in r.data["threads"]] == ["T1", "T2"]
    assert run_cli(repo, "review", "show", "t2", "--json").data["thread"]["github_thread_id"] == "PRRT_b"
    r = run_cli(repo, "review", "show", "T9", "--json")
    assert r.code == 1 and r.data["error"]["code"] == "unknown_thread"


def test_human_output_neutralizes_terminal_control_sequences(repo, fake_gh, run_cli) -> None:
    hostile = "looks fine\x1b[2J\x1b]0;pwned\x07\rREWRITTEN"
    fake_gh.pr(7)["threads"] = [thread("PRRT_a", comment("A1", hostile), path="evil\x1b[31m.py")]
    fake_gh.save()
    run_cli(repo, "refresh")
    shown = run_cli(repo, "review", "show", "T1")
    listed = run_cli(repo, "review", "list")
    for out in (shown.out, listed.out):
        assert "\x1b" not in out and "\x07" not in out and "\r" not in out
    assert "\\x1b[2J" in shown.out
    assert run_cli(repo, "review", "show", "T1", "--json").data["thread"]["comments"][0]["body"] == hostile


def test_detached_head_needs_an_explicit_pr_and_does_not_bind(repo, fake_gh, run_cli) -> None:
    git(repo, "checkout", "-q", "--detach")
    r = run_cli(repo, "refresh", "--json")
    assert r.code == 1 and r.data["error"]["code"] == "detached_head"
    r = run_cli(repo, "refresh", "--pr", "7", "--json")
    assert r.code == 0 and r.data["resolved_by"] == "explicit"
    assert state.read_state(repo / ".git" / "prflow")["branches"] == {}
    assert run_cli(repo, "review", "list", "--json").data["error"]["code"] == "detached_head"
    assert run_cli(repo, "review", "list", "--pr", "7", "--json").code == 0


@pytest.mark.parametrize(
    ("prs", "code", "numbers"),
    [
        ([], "no_pr", None),
        ([pull_request(7, "feature"), pull_request(9, "feature", baseRefName="release")], "pr_ambiguous", [7, 9]),
        ([pull_request(7, "feature", isCrossRepository=True, headOwner="mallory")], "pr_ambiguous", [7]),
    ],
    ids=["none", "two-same-repo", "fork-only"],
)
def test_branch_pr_resolution_never_guesses(repo, fake_gh, run_cli, prs, code, numbers) -> None:
    fake_gh.scenario["prs"] = prs
    fake_gh.save()
    r = run_cli(repo, "status", "--json")
    assert r.code == 1 and r.data["error"]["code"] == code
    if numbers:
        assert [c["number"] for c in r.data["error"]["details"]["candidates"]] == numbers
    assert not (repo / ".git" / "prflow").exists()


def test_configured_upstream_branch_name_is_used_for_pr_lookup(repo, fake_gh, run_cli) -> None:
    git(repo, "config", "branch.feature.merge", "refs/heads/remote-feature")
    fake_gh.scenario["prs"] = [pull_request(12, "remote-feature")]
    fake_gh.save()
    assert run_cli(repo, "refresh", "--json").data["pr"]["pr_number"] == 12


def test_missing_gh_auth_repo_and_pr_are_structured_errors(repo, fake_gh, run_cli, monkeypatch, tmp_path) -> None:
    fake_gh.scenario["fail"] = {"repo view": {"stderr": "To get started with GitHub CLI, please run:  gh auth login"}}
    fake_gh.save()
    r = run_cli(repo, "refresh", "--json")
    assert r.data["error"]["code"] == "gh_auth" and "gh auth login" in r.data["error"]["hint"]

    fake_gh.scenario.pop("fail")
    fake_gh.scenario["repo"] = None
    fake_gh.save()
    assert run_cli(repo, "status", "--json").data["error"]["code"] == "repo_unresolved"

    fake_gh.scenario["repo"] = "acme/widgets"
    fake_gh.save()
    assert run_cli(repo, "refresh", "--pr", "99", "--json").data["error"]["code"] == "pr_not_found"
    assert run_cli(repo, "refresh", "--repo", "acme/nope", "--json").data["error"]["code"] == "repo_not_found"

    only_git = tmp_path / "only-git"
    only_git.mkdir()
    (only_git / "git").symlink_to(shutil.which("git"))
    monkeypatch.setenv("PATH", str(only_git))
    r = run_cli(repo, "refresh", "--json")
    assert r.code == 1 and r.data["error"]["code"] == "gh_missing"


def test_outside_a_repository_and_usage_errors(tmp_path, run_cli, repo) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    r = run_cli(outside, "status", "--json")
    assert r.code == 1 and r.data == {"ok": False, "command": "status", "error": r.data["error"]}
    assert r.data["error"]["code"] == "not_a_git_repo"

    for args in (["review", "show", "--json"], ["refresh", "--pr", "0", "--json"], ["bogus", "--json"]):
        r = run_cli(repo, *args)
        assert r.code == 2 and r.data["error"]["code"] == "usage", args

    r = run_cli(repo, "review", "list")
    assert r.code == 1 and r.out == "" and "prflow: error:" in r.err and "hint:" in r.err


def test_switching_branches_keeps_each_prs_cache_and_aliases_apart(repo, fake_gh, run_cli) -> None:
    fake_gh.pr(7)["threads"] = [thread("PRRT_a", comment("A1")), thread("PRRT_b", comment("B1", created="2026-09-02T00:00:00Z"))]
    fake_gh.scenario["prs"].append(pull_request(8, "other", [thread("PRRT_x", comment("X1"))]))
    fake_gh.save()
    assert run_cli(repo, "refresh", "--json").data["summary"]["new"] == ["T1", "T2"]

    git(repo, "checkout", "-q", "-b", "other")
    assert run_cli(repo, "review", "list", "--json").data["error"]["code"] == "not_refreshed"
    r = run_cli(repo, "refresh", "--json")
    assert r.data["pr"]["pr_number"] == 8 and r.data["summary"]["new"] == ["T1"]
    assert run_cli(repo, "review", "show", "T1", "--json").data["thread"]["github_thread_id"] == "PRRT_x"
    assert run_cli(repo, "review", "show", "T1", "--pr", "7", "--json").data["thread"]["github_thread_id"] == "PRRT_a"

    git(repo, "checkout", "-q", "feature")
    r = run_cli(repo, "review", "list", "--json")
    assert r.data["pr"]["pr_number"] == 7
    assert [(t["alias"], t["github_thread_id"]) for t in r.data["threads"]] == [("T1", "PRRT_a"), ("T2", "PRRT_b")]


def test_expected_revision_is_enforced(repo, fake_gh, run_cli) -> None:
    assert run_cli(repo, "refresh", "--json").data["state_revision"] == 1
    path = repo / ".git" / "prflow" / "state.json"
    before = path.read_bytes()
    r = run_cli(repo, "refresh", "--expected-revision", "0", "--json")
    assert r.code == 1 and r.data["error"]["code"] == "state_revision_mismatch"
    assert path.read_bytes() == before
    assert run_cli(repo, "refresh", "--expected-revision", "1", "--json").data["state_revision"] == 2


def test_corrupted_state_is_reported_and_left_alone(repo, fake_gh, run_cli) -> None:
    directory = repo / ".git" / "prflow"
    directory.mkdir()
    (directory / "state.json").write_text("{oops")
    for args in (["refresh", "--json"], ["review", "list", "--json"], ["status", "--json"]):
        r = run_cli(repo, *args)
        assert r.code == 1 and r.data["error"]["code"] == "state_corrupt", args
    assert (directory / "state.json").read_text() == "{oops"


def test_concurrent_state_update_during_the_network_read_is_kept(repo, fake_gh, monkeypatch) -> None:
    fake_gh.pr(7)["threads"] = [thread("PRRT_a", comment("A1"))]
    fake_gh.save()
    checkout = inspect_checkout(repo)
    real_fetch = github.fetch_review_threads

    def fetch_while_someone_else_writes(*args, **kwargs):
        result = real_fetch(*args, **kwargs)
        with state.transaction(checkout.state_dir) as st:
            st["branches"]["elsewhere"] = {"pr": "acme/widgets#99", "bound_at": "concurrent"}
        return result

    monkeypatch.setattr(github, "fetch_review_threads", fetch_while_someone_else_writes)
    result = review.refresh(checkout)
    final = state.read_state(checkout.state_dir)
    assert result["state_revision"] == final["state_revision"] == 2
    assert final["branches"]["elsewhere"]["bound_at"] == "concurrent"
    assert final["branches"]["feature"]["pr"] == "acme/widgets#7"


def test_status_reports_identity_stack_and_cached_reviews(repo, fake_gh, run_cli) -> None:
    fake_gh.scenario["prs"] = [
        pull_request(7, "feature", [thread("PRRT_a", comment("A1"))], baseRefName="stack-base"),
        pull_request(5, "stack-base"),
        pull_request(11, "feature-child", baseRefName="feature"),
    ]
    fake_gh.save()
    r = run_cli(repo, "status", "--json")
    assert r.code == 0
    assert (r.data["pr"]["pr_number"], r.data["pr"]["stack_parent_pr"], r.data["pr"]["stack_child_prs"]) == (7, 5, [11])
    assert r.data["reviews"] is None and r.data["resolved_by"] == "branch" and r.data["head_matches_local"] is False
    assert not (repo / ".git" / "prflow").exists(), "status never writes state"

    run_cli(repo, "refresh")
    r = run_cli(repo, "status")
    assert "stack    parent #5; children #11" in r.out
    assert "reviews  1 unresolved of 1" in r.out


def test_product_code_has_no_github_mutation_or_shell_path() -> None:
    for path in (SRC / "prflow").rglob("*.py"):
        text = path.read_text()
        for forbidden in ("addPullRequestReviewThreadReply", "resolveReviewThread", "createIssue", "shell=True", "openai_codex import"):
            assert forbidden not in text, (path, forbidden)
    assert all(q.lstrip().startswith("query") for q in (github.PR_QUERY, github.THREADS_QUERY, github.COMMENTS_QUERY))
    with pytest.raises(AssertionError):
        github.graphql("mutation { resolveReviewThread }", {}, ".")


def test_graphql_strings_are_raw_fields(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(github, "gh_json", lambda argv, cwd: seen.append(argv) or {"data": {}})
    github.graphql("query { x }", {"cursor": "@/etc/passwd", "number": 3}, ".")
    assert ["-f", "cursor=@/etc/passwd"] == seen[0][4:6] and ["-F", "number=3"] == seen[0][6:8]
    with pytest.raises(PrflowError):
        github.resolve_repo(".", "--help")


BLOCK_SDK = """
import importlib.abc, sys
class BlockSdk(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('openai_codex', 'codex_cli_bin'):
            raise ImportError('SDK blocked by test')
sys.meta_path.insert(0, BlockSdk())
from prflow import cli
code = cli.main(sys.argv[1:])
assert 'openai_codex' not in sys.modules
sys.exit(code)
"""


def test_entry_points_and_basic_commands_need_no_codex_sdk(repo, fake_gh) -> None:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    version = subprocess.run([sys.executable, "-m", "prflow", "--version"], env=env, capture_output=True, text=True)
    assert version.returncode == 0 and version.stdout.strip() == "prflow 0.1.0"

    def run(*args: str) -> tuple[int, dict]:
        proc = subprocess.run([sys.executable, "-c", BLOCK_SDK, *args], cwd=repo, env=env, capture_output=True, text=True)
        return proc.returncode, json.loads(proc.stdout)

    fake_gh.pr(7)["threads"] = [thread("PRRT_a", comment("A1"))]
    fake_gh.save()
    assert run("review", "list", "--json")[1]["error"]["code"] == "not_refreshed"
    assert run("refresh", "--json")[0] == 0
    assert run("status", "--json")[0] == 0
    code, data = run("review", "list", "--json")
    assert code == 0 and data["threads"][0]["alias"] == "T1"
    code, data = run("doctor", "--json")
    assert code == 0 and data["summary"]["phase1_ready"] is True
    assert {c["name"]: c["status"] for c in data["checks"]}["external_message"] == "unavailable"


@pytest.mark.parametrize("response", [None, [], "not an object"])
def test_non_object_graphql_response_is_a_service_error(monkeypatch, tmp_path, response):
    monkeypatch.setattr(github, "gh_json", lambda *args: response)
    with pytest.raises(PrflowError) as caught:
        github.graphql("query { viewer { login } }", {}, tmp_path)
    assert caught.value.code == "gh_failed"
