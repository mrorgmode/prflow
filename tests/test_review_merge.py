"""Pure review-thread logic: fingerprints, normalization, aliases, §11.6 refresh semantics."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from phase1_support import comment, thread
from prflow import review, state

# The Phase 0 spike is used here only as an independent oracle for the §10.2 algorithm.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spikes"))
import spike_d_graphql as phase0  # noqa: E402

KEY = "acme/widgets#7"


def merge(st: dict[str, Any], threads: list[dict[str, Any]], fetched_at: str, *, pr: int = 7, branch: str | None = "feature") -> dict[str, Any]:
    context = {"repo": "acme/widgets", "pr_number": pr, "branch": branch, "base_branch": "main", "head_sha": "abc"}
    return review.merge_snapshot(st, context, [review.normalize_thread(t) for t in threads], fetched_at, branch)


def aliases(st: dict[str, Any], key: str = KEY) -> dict[str, str | None]:
    return {tid: t["alias"] for tid, t in st["prs"][key]["threads"].items()}


def test_fingerprint_matches_phase0_algorithm_and_ignores_comment_order() -> None:
    node = thread("PRRT_1", comment("C2"), comment("C1", updated="2026-09-02T00:00:00Z"), outdated=True)
    record = review.normalize_thread(node)
    assert record["source_fingerprint"] == phase0.source_fingerprint(record)
    swapped = review.normalize_thread({**node, "comments": list(reversed(node["comments"]))})
    assert swapped["source_fingerprint"] == record["source_fingerprint"]


@pytest.mark.parametrize(
    "change",
    [
        lambda n: n["comments"].append(comment("C9", created="2026-09-03T00:00:00Z")),
        lambda n: n["comments"][0].update(updatedAt="2026-09-05T00:00:00Z"),
        lambda n: n.update(isResolved=True),
        lambda n: n.update(isOutdated=True),
    ],
    ids=["new-comment", "edited-comment", "resolved", "outdated"],
)
def test_fingerprint_changes_for_each_staleness_signal(change) -> None:
    node = thread("PRRT_1", comment("C1"), comment("C2"))
    before = review.normalize_thread(node)["source_fingerprint"]
    change(node)
    assert review.normalize_thread(node)["source_fingerprint"] != before


def test_fingerprint_ignores_body_text_without_updated_at() -> None:
    node = thread("PRRT_1", comment("C1", "one"))
    before = review.normalize_thread(node)["source_fingerprint"]
    node["comments"][0]["body"] = "two"
    assert review.normalize_thread(node)["source_fingerprint"] == before


def test_normalize_accepts_deleted_author_and_null_line_context() -> None:
    node = thread("PRRT_1", comment("C1", author=None), outdated=True, line=None, originalLine=None, path=None, subjectType="FILE")
    record = review.normalize_thread(node)
    assert record["comments"][0]["author"] is None
    assert record["line"] is None and record["original_line"] is None and record["path"] is None
    assert record["diff_hunk"]


def test_aliases_are_stable_and_never_reused_or_renumbered() -> None:
    st = state.empty_state()
    a = thread("PRRT_a", comment("A1", created="2026-09-01T00:00:00Z"))
    b = thread("PRRT_b", comment("B1", created="2026-09-02T00:00:00Z"))
    c = thread("PRRT_c", comment("C1", created="2026-08-01T00:00:00Z"))  # older than a, but seen later
    assert merge(st, [b, a], "2026-09-10T00:00:01Z")["new"] == ["T1", "T2"]
    assert aliases(st) == {"PRRT_a": "T1", "PRRT_b": "T2"}

    summary = merge(st, [c, b], "2026-09-10T00:00:02Z")
    assert summary["new"] == ["T3"] and summary["missing"] == ["T1"]
    assert st["prs"][KEY]["threads"]["PRRT_a"]["present_on_github"] is False

    summary = merge(st, [a, b, c], "2026-09-10T00:00:03Z")
    assert summary["new"] == []
    assert aliases(st) == {"PRRT_a": "T1", "PRRT_b": "T2", "PRRT_c": "T3"}
    assert st["prs"][KEY]["threads"]["PRRT_a"]["present_on_github"] is True


def test_thread_first_seen_resolved_gets_an_alias_only_once_unresolved() -> None:
    st = state.empty_state()
    old = thread("PRRT_old", comment("O1"), resolved=True)
    merge(st, [old], "2026-09-10T00:00:01Z")
    assert aliases(st) == {"PRRT_old": None}
    merge(st, [thread("PRRT_new", comment("N1"))], "2026-09-10T00:00:02Z")
    old["isResolved"] = False
    summary = merge(st, [old, thread("PRRT_new", comment("N1"))], "2026-09-10T00:00:03Z")
    assert summary["reopened"] == ["T2"]
    assert st["prs"][KEY]["threads"]["PRRT_old"]["state"] == "discovered"


def test_external_resolution_completes_untouched_items_and_reopen_restores_them() -> None:
    st = state.empty_state()
    node = thread("PRRT_1", comment("C1"))
    merge(st, [node], "2026-09-10T00:00:01Z")
    node["isResolved"] = True
    summary = merge(st, [node], "2026-09-10T00:00:02Z")
    record = st["prs"][KEY]["threads"]["PRRT_1"]
    assert (record["state"], record["state_reason"]) == ("done", "resolved_on_github")
    assert summary["resolved"] == [{"thread": "T1", "state": "done"}]
    assert summary["changed"] == ["T1"]

    node["isResolved"] = False
    summary = merge(st, [node], "2026-09-10T00:00:03Z")
    assert summary["reopened"] == ["T1"]
    assert (record["alias"], record["state"], record["state_reason"]) == ("T1", "discovered", None)


def test_resolution_during_local_work_marks_stale_and_preserves_everything() -> None:
    st = state.empty_state()
    node = thread("PRRT_1", comment("C1"))
    merge(st, [node], "2026-09-10T00:00:01Z")
    record = st["prs"][KEY]["threads"]["PRRT_1"]
    record["state"] = "working"
    record["local_note"] = "half-finished fix"  # stands in for future local work fields
    node["isResolved"] = True
    merge(st, [node], "2026-09-10T00:00:02Z")
    assert (record["state"], record["state_reason"]) == ("stale", "resolved_on_github")
    assert record["local_note"] == "half-finished fix"
    node["isResolved"] = False
    merge(st, [node], "2026-09-10T00:00:03Z")
    assert record["state"] == "stale", "reopening must not silently discard the need for human reconciliation"


def test_older_snapshot_never_overwrites_a_newer_one() -> None:
    st = state.empty_state()
    merge(st, [thread("PRRT_1", comment("C1"))], "2026-09-10T00:00:05Z")
    stale = merge(st, [thread("PRRT_1", comment("C1")), thread("PRRT_2", comment("C2"))], "2026-09-10T00:00:01Z")
    assert stale["superseded"] is True
    assert list(st["prs"][KEY]["threads"]) == ["PRRT_1"]


def test_each_pr_has_its_own_aliases_and_the_branch_rebinds() -> None:
    st = state.empty_state()
    merge(st, [thread("PRRT_a", comment("A1"))], "2026-09-10T00:00:01Z", pr=7)
    summary = merge(st, [thread("PRRT_x", comment("X1"))], "2026-09-10T00:00:02Z", pr=8)
    assert summary["rebound_from"] == KEY
    assert st["branches"]["feature"]["pr"] == "acme/widgets#8"
    assert aliases(st, "acme/widgets#8") == {"PRRT_x": "T1"}
    assert aliases(st, KEY) == {"PRRT_a": "T1"}, "the other PR's cache stays intact"


def test_explicit_selection_does_not_bind_the_branch() -> None:
    st = state.empty_state()
    merge(st, [], "2026-09-10T00:00:01Z", pr=9, branch=None)
    assert st["branches"] == {}
