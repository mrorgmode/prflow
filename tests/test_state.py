"""Per-worktree state location, schema/revision validation, locking and atomic writes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from phase1_support import git
from prflow import state
from prflow.errors import PrflowError
from prflow.git import inspect_checkout

SRC = Path(__file__).resolve().parents[1] / "src"
pytestmark = pytest.mark.usefixtures("isolated_env")


def test_missing_state_reads_as_empty_without_creating_anything(tmp_path: Path) -> None:
    directory = tmp_path / "prflow"
    assert state.read_state(directory)["state_revision"] == 0
    assert not directory.exists()


def test_transaction_bumps_revision_only_when_content_changes(tmp_path: Path) -> None:
    directory = tmp_path / "prflow"
    with state.transaction(directory) as st:
        st["branches"]["feature"] = {"pr": "acme/widgets#7", "bound_at": "t"}
    assert st["state_revision"] == 1
    with state.transaction(directory) as st:
        pass
    assert st["state_revision"] == 1
    on_disk = json.loads((directory / "state.json").read_text())
    assert on_disk["schema_version"] == state.SCHEMA_VERSION and on_disk["state_revision"] == 1
    assert not list(directory.glob(".state.*")), "temporary files must not be left behind"


def test_stale_expected_revision_fails_without_writing(tmp_path: Path) -> None:
    directory = tmp_path / "prflow"
    with state.transaction(directory) as st:
        st["branches"]["a"] = {"pr": "x#1", "bound_at": "t"}
    before = (directory / "state.json").read_bytes()
    with pytest.raises(PrflowError) as excinfo:
        with state.transaction(directory, expected_revision=0) as st:
            st["branches"]["b"] = {"pr": "x#2", "bound_at": "t"}
    assert excinfo.value.code == "state_revision_mismatch"
    assert excinfo.value.details == {"expected_revision": 0, "state_revision": 1}
    assert (directory / "state.json").read_bytes() == before
    with state.transaction(directory, expected_revision=1) as st:
        st["branches"]["b"] = {"pr": "x#2", "bound_at": "t"}
    assert st["state_revision"] == 2


def test_exception_inside_transaction_writes_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "prflow"
    with pytest.raises(RuntimeError):
        with state.transaction(directory) as st:
            st["branches"]["a"] = {"pr": "x#1", "bound_at": "t"}
            raise RuntimeError("boom")
    assert not (directory / "state.json").exists()


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("{not json", "state_corrupt"),
        ("[]", "state_corrupt"),
        ('{"schema_version": 1, "state_revision": "3", "branches": {}, "prs": {}}', "state_corrupt"),
        ('{"schema_version": 1, "state_revision": 2}', "state_corrupt"),
        ('{"schema_version": 99, "state_revision": 1, "branches": {}, "prs": {}}', "state_future_schema"),
    ],
)
def test_unusable_state_is_rejected_and_never_overwritten(tmp_path: Path, content: str, code: str) -> None:
    directory = tmp_path / "prflow"
    directory.mkdir()
    (directory / "state.json").write_text(content)
    with pytest.raises(PrflowError) as excinfo:
        state.read_state(directory)
    assert excinfo.value.code == code
    with pytest.raises(PrflowError):
        with state.transaction(directory) as st:
            st["branches"]["a"] = {}
    assert (directory / "state.json").read_text() == content


WRITER = """
import sys
from pathlib import Path
from prflow import state
for _ in range(int(sys.argv[2])):
    with state.transaction(Path(sys.argv[1])) as st:
        st["test_counter"] = st.get("test_counter", 0) + 1
"""


def test_concurrent_writers_do_not_lose_updates(tmp_path: Path) -> None:
    directory = tmp_path / "prflow"
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(directory), "25"], env=env) for _ in range(4)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    final = state.read_state(directory)
    assert final["test_counter"] == 100
    assert final["state_revision"] == 100


def test_state_dir_is_stable_from_subdirectories(repo: Path) -> None:
    (repo / "pkg" / "deep").mkdir(parents=True)
    top = inspect_checkout(repo)
    nested = inspect_checkout(repo / "pkg" / "deep")
    assert top.state_dir == nested.state_dir == (repo / ".git" / "prflow")
    assert top.root == nested.root == repo


def test_linked_worktree_gets_its_own_state(repo: Path) -> None:
    linked_root = repo.parent / "widgets-wt"
    git(repo, "worktree", "add", "-q", "-b", "other", str(linked_root))
    assert (linked_root / ".git").is_file()
    (linked_root / "sub").mkdir()
    linked = inspect_checkout(linked_root / "sub")
    main = inspect_checkout(repo)
    assert linked.state_dir == repo / ".git" / "worktrees" / "widgets-wt" / "prflow"
    assert linked.branch == "other" and main.branch == "feature"
    with state.transaction(linked.state_dir) as st:
        st["branches"]["other"] = {"pr": "acme/widgets#8", "bound_at": "t"}
    assert state.read_state(main.state_dir)["state_revision"] == 0


def test_detached_head_and_configured_upstream_branch(repo: Path) -> None:
    git(repo, "config", "branch.feature.merge", "refs/heads/remote-name")
    assert inspect_checkout(repo).upstream_branch == "remote-name"
    git(repo, "checkout", "-q", "--detach")
    detached = inspect_checkout(repo)
    assert detached.branch is None and detached.head and detached.to_json()["detached"]


def test_outside_git_is_a_structured_error(tmp_path: Path) -> None:
    with pytest.raises(PrflowError) as excinfo:
        inspect_checkout(tmp_path)
    assert excinfo.value.code == "not_a_git_repo"


@pytest.mark.parametrize("entry", [
    {"next_alias": 0, "threads": {}},
    {"next_alias": 1, "threads": {"a": {"github_thread_id": "a", "alias": "T1"}}},
    {"next_alias": 2, "threads": {
        "a": {"github_thread_id": "a", "alias": "T1"},
        "b": {"github_thread_id": "b", "alias": "T1"},
    }},
])
def test_corrupt_alias_state_cannot_be_overwritten(tmp_path, entry):
    snapshot = state.empty_state()
    snapshot["prs"]["acme/widgets#7"] = entry
    target = tmp_path / state.STATE_FILE
    target.write_text(json.dumps(snapshot))
    before = target.read_bytes()
    with pytest.raises(PrflowError, match="alias"):
        with state.transaction(tmp_path):
            pytest.fail("invalid alias state must be rejected before mutation")
    assert target.read_bytes() == before
