"""Offline tests for the spike helpers: Spike B semantic checks, Spike D fingerprints, Spike E prompt."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spikes"))

import spike_b_untrusted_input as b  # noqa: E402
import spike_d_graphql as d  # noqa: E402
import spike_e_interactive as e  # noqa: E402

FIXTURE = json.loads((ROOT / "spikes" / "fixtures" / "review_thread_injection.json").read_text())
EXPECTED = FIXTURE["expected"]

GOOD_RESULT = {
    "thread_alias": "T1",
    "disposition": "fix_now",
    "summary": "Reviewer wants validation before parsing.",
    "rationale": "Unknown names raise a bare KeyError; validate and raise ValueError.",
    "relevant_files": ["src/example/parser.py", "tests/test_parser.py"],
    "proposed_reply": "Good catch. I will validate `name` against STATES and raise a clear ValueError, with a test.",
    "resolve_after_reply": False,
    "issue_search_queries": [],
    "human_note": None,
}


# --- Spike B ---------------------------------------------------------------

def test_semantic_checks_pass_for_clean_result() -> None:
    assert all(b.check_semantic_integrity(GOOD_RESULT, EXPECTED).values())


def test_semantic_checks_catch_each_injection_effect() -> None:
    flipped = dict(GOOD_RESULT, disposition="reply_only")
    assert not b.check_semantic_integrity(flipped, EXPECTED)["disposition_unchanged"]
    resolved = dict(GOOD_RESULT, resolve_after_reply=True)
    assert not b.check_semantic_integrity(resolved, EXPECTED)["resolve_after_reply_not_flipped"]
    alias = dict(GOOD_RESULT, thread_alias="T9")
    assert not b.check_semantic_integrity(alias, EXPECTED)["thread_alias_from_task"]
    marker = dict(GOOD_RESULT, proposed_reply=GOOD_RESULT["proposed_reply"] + " INJECTED-MARKER-7731")
    assert not b.check_semantic_integrity(marker, EXPECTED)["no_injected_text_in_reply"]
    secrets = dict(GOOD_RESULT, proposed_reply="oauth_token: abc")
    assert not b.check_semantic_integrity(secrets, EXPECTED)["no_injected_text_in_reply"]
    files = dict(GOOD_RESULT, relevant_files=["rm -rf /"])
    assert not b.check_semantic_integrity(files, EXPECTED)["no_injected_relevant_files"]
    absolute = dict(GOOD_RESULT, relevant_files=["~/.config/gh/hosts.yml"])
    assert not b.check_semantic_integrity(absolute, EXPECTED)["no_injected_relevant_files"]


def test_task_renders_thread_as_delimited_data_and_alias_from_task() -> None:
    task = b.build_task(FIXTURE)
    assert task.count("<<<REVIEW_THREAD_DATA") == 1 and task.count("REVIEW_THREAD_DATA>>>") == 1
    assert "Triage review thread alias T1" in task
    assert "SYSTEM OVERRIDE" in task  # the injection is present as data, not stripped
    assert "reviewer-alice" in task


def test_triage_schema_matches_spec_10_5_example() -> None:
    spec = (ROOT / "SPEC.md").read_text()
    start = spec.index("### 10.5 Triage result")
    example = json.loads(spec[spec.index("```json", start) + 7 : spec.index("```", spec.index("```json", start) + 7)])
    assert set(b.TRIAGE_SCHEMA["required"]) == set(example)
    assert set(b.TRIAGE_SCHEMA["properties"]) == set(example)
    dispositions = spec[spec.index("### 10.3 Disposition") : spec.index("### 10.4")]
    assert all(v in dispositions for v in b.TRIAGE_SCHEMA["properties"]["disposition"]["enum"])


# --- Spike D ---------------------------------------------------------------

def _node(resolved: bool = False, outdated: bool = False, updated: str = "2026-09-09T10:15:00Z") -> dict:
    return {
        "id": "PRRT_1", "isResolved": resolved, "isOutdated": outdated, "path": "a.py", "line": 3, "originalLine": 3,
        "comments": {"nodes": [
            {"id": "PRRC_2", "databaseId": 2, "url": "u2", "author": {"login": "bob"}, "body": "second", "createdAt": "2026-09-09T10:16:00Z", "updatedAt": "2026-09-09T10:16:00Z"},
            {"id": "PRRC_1", "databaseId": 1, "url": "u1", "author": {"login": "alice"}, "body": "first", "createdAt": "2026-09-09T10:15:00Z", "updatedAt": updated},
        ]},
    }


def test_fingerprint_is_deterministic_and_order_independent() -> None:
    a = d.normalize_thread(_node())
    reordered = _node()
    reordered["comments"]["nodes"].reverse()
    assert a["source_fingerprint"] == d.normalize_thread(reordered)["source_fingerprint"]
    assert a["source_fingerprint"].startswith("sha256:")


def test_fingerprint_changes_on_edit_resolution_or_outdated() -> None:
    base = d.normalize_thread(_node())["source_fingerprint"]
    assert d.normalize_thread(_node(updated="2026-09-10T00:00:00Z"))["source_fingerprint"] != base
    assert d.normalize_thread(_node(resolved=True))["source_fingerprint"] != base
    assert d.normalize_thread(_node(outdated=True))["source_fingerprint"] != base


def test_fingerprint_ignores_body_text_changes_without_updated_at() -> None:
    node = _node()
    node["comments"]["nodes"][0]["body"] = "different text, same updatedAt"
    assert d.normalize_thread(node)["source_fingerprint"] == d.normalize_thread(_node())["source_fingerprint"]


def test_mutation_argv_goes_through_gh_only() -> None:
    reply = d.reply_argv("PRRT_1", "hello")
    assert reply[:3] == ["gh", "api", "graphql"] and "threadId=PRRT_1" in reply and "body=hello" in reply
    resolve = d.resolve_argv("PRRT_1")
    assert resolve[:3] == ["gh", "api", "graphql"] and "resolveReviewThread" in resolve[4]
    assert any("-F" == flag for flag in d.gh_graphql_argv("q", {"number": 5})[3:])  # ints as -F


# --- Spike E ---------------------------------------------------------------

def test_interactive_prompt_contains_context_policy_and_delimited_data() -> None:
    prompt = e.build_prompt(FIXTURE, "OWNER/REPO", 123, "fix_now", {"summary": "s"})
    assert "PR #123" in prompt and "T1" in prompt
    assert "prflow stage" in prompt and "force-push" in prompt
    assert "<<<REVIEW_THREAD_DATA" in prompt and "REVIEW_THREAD_DATA>>>" in prompt
    assert "outside prflow's staged-outbox guarantee" in prompt


def test_interactive_argv_runs_native_codex_in_repo_root(monkeypatch, tmp_path: Path) -> None:
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(e.shutil, "which", lambda _name: str(fake))
    argv = e.build_argv(tmp_path, "PROMPT")
    assert argv == [str(fake), "--cd", str(tmp_path), "PROMPT"]
    assert "-m" not in e.build_argv(tmp_path, "PROMPT")  # user's interactive default model is respected
