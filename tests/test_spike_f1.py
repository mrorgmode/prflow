"""Offline tests for the Spike F.1 `:workspace`-extending profile harness (no Codex, no network)."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spikes"))

import spike_f1_workspace_profile as f1  # noqa: E402
import spike_f_credential_read as sf  # noqa: E402
import spike_f_probe as probe  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args], cwd=cwd, env=f1.GIT_ENV, check=True, capture_output=True, text=True).stdout


def _repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo, wt = tmp_path / "repo", tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "--template=", "-b", "main")
    (repo / "README.md").write_text("x\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
    return repo, wt


def test_profile_extends_workspace_and_narrows_it(tmp_path: Path) -> None:
    release, common = tmp_path / "rel", tmp_path / "repo/.git"
    profile = f1.build_workspace_profile(codex_home=tmp_path / "ch", runtime_read_roots=(release,), git_read_roots=(common,))
    fs = profile["filesystem"]
    assert profile["extends"] == ":workspace" and profile["network"] == {"enabled": False}
    assert fs[":root"] == "deny" and fs[":minimal"] == "read"
    assert fs[":tmpdir"] == "deny" and fs[":slash_tmp"] == "deny"
    assert fs[str(release)] == "read" and fs[str(common)] == "read"  # read-only exceptions, never write
    assert "write" not in {v for k, v in fs.items() if k != ":workspace_roots"}
    assert fs[":workspace_roots"] == {"**/.env": "deny", "**/.env.*": "deny"}  # "." write comes from :workspace
    for path in ("~/.config/gh", "~/.ssh", "~/.gnupg", "~/.codex/auth.json", str(tmp_path / "ch/auth.json")):
        assert fs[path] == "deny"
    assert ":tmpdir" not in f1.build_workspace_profile(codex_home=tmp_path, runtime_read_roots=(), deny_tmp=False)["filesystem"]
    overrides = sf.profile_batch_overrides(profile)
    keys = [kv.split("=", 1)[0] for kv in overrides]
    assert not set(keys) & set(sf.LEGACY_SANDBOX_KEYS)
    assert "mcp_servers" in keys and overrides[-1] == f'default_permissions="{sf.PROFILE_ID}"'


def test_git_metadata_and_read_roots_for_normal_and_linked_worktree(tmp_path: Path) -> None:
    repo, wt = _repo_with_worktree(tmp_path)
    normal = f1.git_metadata(repo)
    assert normal["git_dir"] == normal["common_dir"] == str(repo / ".git")
    assert normal["git_path_prflow"] == str(repo / ".git/prflow")
    assert f1.git_metadata_read_roots(normal, repo) == ()  # covered (read-only) by :workspace
    linked = f1.git_metadata(wt)
    assert (wt / ".git").is_file() and linked["toplevel"] == str(wt)
    assert linked["git_dir"] == str(repo / ".git/worktrees/wt") and linked["common_dir"] == str(repo / ".git")
    assert linked["git_path_prflow"] == str(repo / ".git/worktrees/wt/prflow")  # per-worktree state path
    assert f1.git_metadata_read_roots(linked, wt) == (repo / ".git",)  # git dir collapses into common dir


def test_read_roots_keep_separate_dirs_and_drop_workspace_contained(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    meta = {"git_dir": str(tmp_path / "a/gd"), "common_dir": str(tmp_path / "b/common")}
    assert f1.git_metadata_read_roots(meta, ws) == (tmp_path / "a/gd", tmp_path / "b/common")
    inside = {"git_dir": str(ws / ".git"), "common_dir": str(tmp_path / "b/common")}
    assert f1.git_metadata_read_roots(inside, ws) == (tmp_path / "b/common",)


def test_write_attempts_classify_without_changing_content(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses permissions")
    existing = tmp_path / "existing"
    existing.write_text("keep\n")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        assert probe.write_attempt({"path": str(existing), "op": "append"}) == "WRITABLE"
        assert probe.write_attempt({"path": str(tmp_path / "new"), "op": "create"}) == "WRITABLE"
        assert probe.write_attempt({"path": str(tmp_path / "newdir"), "op": "mkdir"}) == "WRITABLE"
        assert probe.write_attempt({"path": str(locked / "x"), "op": "create"}) == "BLOCKED"
        assert probe.write_attempt({"path": str(tmp_path / "absent/x"), "op": "create"}) == "MISSING"
        assert probe.write_attempt({"path": str(existing), "op": "create"}) == "ERROR:EEXIST"
        assert probe.write_attempt({"path": str(existing), "op": "chmod"}) == "ERROR:BAD_OP"
    finally:
        locked.chmod(0o700)
    assert existing.read_text() == "keep\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["existing", "locked"]  # nothing left behind
    assert probe._write_status(OSError(errno.EROFS, "ro")) == "READONLY"


def test_probe_git_checks_in_real_worktree_restore_config(tmp_path: Path) -> None:
    repo, wt = _repo_with_worktree(tmp_path)
    spec = {"targets": [], "write_targets": [], "git": True}
    proc = subprocess.run([sys.executable, "-I", "-S", str(sf.PROBE_SRC), json.dumps(spec)], cwd=wt, env=f1.GIT_ENV, capture_output=True, text=True, timeout=60)
    run = f1.checked(sf.parse_probe_output(proc.stdout, proc.returncode, proc.stderr))
    assert run.ok, run.reason
    assert set(run.data["git_ops"].values()) == {"OK"}
    assert run.data["git_write_ops"] == {"git_config_set": "OK"}  # unsandboxed: positive control
    assert "prflow" not in (repo / ".git/config").read_text()  # removed again
    assert run.data["git_paths"] == f1.git_metadata(wt)


def _probe(write: str = "READONLY", meta: str = "READABLE", cred: str = "BLOCKED", **extra) -> sf.ProbeRun:
    data = {
        "targets": [{"id": "c", "kind": "file", "read": cred}, {"id": "m", "kind": "file", "read": meta}],
        "write_targets": [{"id": "w", "write": write}, {"id": "ws", "write": "WRITABLE"}],
        "workspace_ops": {"git_status": "OK"},
        "git_ops": {"git_diff": "OK"},
        "git_write_ops": {"git_config_set": "EXIT255"},
        "git_paths": {"git_dir": "/g"},
        "network": "BLOCKED",
    }
    data.update(extra)
    return sf.ProbeRun(True, None, data, 0, [])


TARGETS = [{"id": "c", "category": "credential"}, {"id": "m", "category": "git_meta"}]
WRITES = [{"id": "w", "category": "git_meta_write"}, {"id": "ws", "category": "ws_write"}]
PATHS = {"git_dir": "/g"}


def _verdict(run: sf.ProbeRun, parent: sf.ProbeRun | None = None) -> dict:
    parent = parent or _probe(write="WRITABLE", cred="READABLE", git_write_ops={"git_config_set": "OK"})
    baseline = _probe(cred="READABLE")
    return f1.f1_verdict(parent, baseline, run, TARGETS, WRITES, PATHS)


def test_f1_verdict_pass_and_each_failure_mode() -> None:
    assert _verdict(_probe())["verdict"] == "PASS"
    assert _verdict(_probe(write="BLOCKED"))["verdict"] == "PASS"
    assert "write:w:WRITABLE" in _verdict(_probe(write="WRITABLE"))["failures"]
    assert "meta_read:m:MISSING" in _verdict(_probe(meta="MISSING"))["failures"]
    assert "credential:c:EXPOSED" in _verdict(_probe(cred="READABLE"))["failures"]
    assert _verdict(_probe(git_ops={"git_status": "EXIT128"}))["verdict"] == "FAIL"
    assert "git_write:git_config_set:OK" in _verdict(_probe(git_write_ops={"git_config_set": "OK"}))["failures"]
    assert "git_paths_mismatch" in _verdict(_probe(git_paths=None))["failures"]
    assert _verdict(_probe(network="CONNECTED"))["verdict"] == "FAIL"
    assert _verdict(_probe(network="ERROR:timeout"))["verdict"] == "INCONCLUSIVE"
    assert _verdict(sf.ProbeRun(False, "no_begin_sentinel", None, 1, []))["verdict"] == "BROKEN"


def test_f1_verdict_needs_writable_positive_controls() -> None:
    parent_blocked = _probe(write="READONLY", cred="READABLE", git_write_ops={"git_config_set": "OK"})
    out = _verdict(_probe(), parent_blocked)
    assert out["verdict"] == "INCONCLUSIVE" and "write_control:w:READONLY" in out["inconclusive"]
    broken_parent = sf.ProbeRun(False, "no_begin_sentinel", None, 1, [])
    assert _verdict(_probe(), broken_parent)["verdict"] == "INCONCLUSIVE"
    missing_file = _probe(write="MISSING")  # a missing target is a failure, never "blocked"
    assert "write:w:MISSING" in _verdict(missing_file)["failures"]


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"write_targets": None}, "write_targets_malformed"),
        ({"write_targets": [{"id": "w"}]}, "write_targets_malformed"),
        ({"git_ops": {}}, "git_ops_malformed"),
        ({"git_write_ops": {"x": 1}}, "git_write_ops_malformed"),
        ({"git_paths": ["/g"]}, "git_paths_malformed"),
    ],
)
def test_checked_rejects_malformed_f1_sections(patch, reason) -> None:
    run = f1.checked(_probe(**patch))
    assert not run.ok and run.data is None and run.reason == f"bad_structure:{reason}"


def test_active_profile_must_name_profile_and_workspace_parent() -> None:
    assert f1.active_profile_ok({"id": sf.PROFILE_ID, "extends": ":workspace"})
    assert not f1.active_profile_ok({"id": sf.PROFILE_ID, "extends": None})
    assert not f1.active_profile_ok({"id": ":workspace", "extends": None})
    assert not f1.active_profile_ok(None)


def test_layout_has_real_linked_worktree_state_and_targets(tmp_path: Path) -> None:
    lay = f1.build_layout(tmp_path / "t")
    normal, wt = lay.checkouts["normal"], lay.checkouts["linked_worktree"]
    assert (normal / ".git").is_dir() and (wt / ".git").is_file()
    for co in (normal, wt):
        meta = f1.git_metadata(co)
        assert (Path(meta["git_path_prflow"]) / "state.json").is_file()
        assert oct((co / ".env").stat().st_mode & 0o777) == "0o600"
        targets, writes = f1.checkout_targets(lay, co, meta)
        assert {t["id"] for t in targets} >= {"gh_hosts", "ws_env", "meta_common_config", "meta_prflow_state"}
        assert {w["id"] for w in writes} >= {"config_modify", "head_modify", "hooks_create", "prflow_state_modify", "ws_file_create"}
        spec = json.loads(f1.f1_spec(targets, writes))
        assert spec["git"] and all("category" not in w for w in spec["write_targets"])
    _, wt_writes = f1.checkout_targets(lay, wt, f1.git_metadata(wt))
    assert {"gitfile_modify", "common_head_modify"} <= {w["id"] for w in wt_writes}
    assert f1.INHERITED_MCP in (lay.codex_home / "config.toml").read_text()
