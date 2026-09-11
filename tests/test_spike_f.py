"""Offline tests for the Spike F credential-read probe and its classification (no Codex, no network)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spikes"))

import spike_f_credential_read as sf  # noqa: E402
import spike_f_probe as probe  # noqa: E402

SECRET = "PRFLOW_TEST_CANARY_VALUE_4242"


def _out(data: dict) -> str:
    return f"noise\n{probe.BEGIN}\n{json.dumps(data)}\n{probe.END}\n"


def test_parse_requires_both_sentinels_single_json_and_zero_exit() -> None:
    good = sf.parse_probe_output(_out({"targets": []}), 0)
    assert good.ok and good.data == {"targets": []}
    assert sf.parse_probe_output("", 1, "bwrap: execvp ...: No such file or directory").reason == "no_begin_sentinel"
    assert sf.parse_probe_output(f"{probe.BEGIN}\n{{}}\n", 0).reason == "no_end_sentinel"
    assert sf.parse_probe_output(f"{probe.BEGIN}\nnot json\n{probe.END}\n", 0).reason == "bad_json"
    assert sf.parse_probe_output(f"{probe.BEGIN}\n{probe.END}\n", 0).reason == "expected_one_json_line_got_0"
    crashed = sf.parse_probe_output(_out({"targets": []}), 3)
    assert not crashed.ok and crashed.reason == "nonzero_exit:3"


def test_broken_run_is_never_counted_as_blocked() -> None:
    broken = sf.parse_probe_output("", 1, "Error: sandbox failed to start")
    good = sf.parse_probe_output(_out({"targets": [{"id": "k", "kind": "file", "read": "READABLE"}]}), 0)
    rows = sf.compare(good, broken, [{"id": "k", "category": "credential"}])
    assert rows[0]["verdict"] == "INCONCLUSIVE_RUN_BROKEN"
    assert sf.mechanism_verdict(broken, rows) == "BROKEN"


@pytest.mark.parametrize(
    ("baseline", "protected", "verdict"),
    [
        ("READABLE", "BLOCKED", "PROTECTED"),
        ("READABLE", "READABLE_EMPTY", "PROTECTED_MASKED"),
        ("READABLE", "MISSING", "HIDDEN"),
        ("READABLE", "READABLE", "EXPOSED"),
        ("LISTABLE", "BLOCKED", "PROTECTED"),
        ("LISTABLE", "LISTABLE", "EXPOSED"),
        ("MISSING", "MISSING", "NOT_PRESENT"),
        ("MISSING", "READABLE", "INCONCLUSIVE_CONTROL_MISSING"),
        ("BLOCKED", "BLOCKED", "INCONCLUSIVE_CONTROL_UNREADABLE"),
        ("READABLE", "ERROR:EIO", "INCONCLUSIVE_ERROR"),
        (None, "BLOCKED", "INCONCLUSIVE_RUN_BROKEN"),
    ],
)
def test_classification_distinguishes_missing_blocked_readable(baseline, protected, verdict) -> None:
    assert sf.classify_target(baseline, protected) == verdict


def _run(rows: list[dict], **extra) -> sf.ProbeRun:
    return sf.ProbeRun(True, None, {"targets": rows, **extra}, 0, [])


def test_mechanism_verdict_requires_protection_workspace_and_no_egress() -> None:
    targets = [{"id": "a", "category": "credential"}, {"id": "g", "category": "general"}]
    base = _run([{"id": "a", "kind": "file", "read": "READABLE"}, {"id": "g", "kind": "file", "read": "READABLE"}])
    ok = _run([{"id": "a", "kind": "file", "read": "BLOCKED"}, {"id": "g", "kind": "file", "read": "READABLE"}], workspace_ops={"git_status": "OK"}, network="BLOCKED")
    assert sf.mechanism_verdict(ok, sf.compare(base, ok, targets)) == "PASS"  # general reads do not decide
    leak = _run([{"id": "a", "kind": "file", "read": "READABLE"}])
    assert sf.mechanism_verdict(leak, sf.compare(base, leak, targets)) == "FAIL"
    ws = _run([{"id": "a", "kind": "file", "read": "BLOCKED"}], workspace_ops={"git_status": "EXIT128"})
    assert sf.mechanism_verdict(ws, sf.compare(base, ws, targets)) == "FAIL_WORKSPACE"
    net = _run([{"id": "a", "kind": "file", "read": "BLOCKED"}], workspace_ops={"git_status": "OK"}, network="CONNECTED")
    assert sf.mechanism_verdict(net, sf.compare(base, net, targets)) == "FAIL_NETWORK"
    absent_base = _run([{"id": "a", "kind": "file", "read": "MISSING"}])
    absent = _run([{"id": "a", "kind": "file", "read": "MISSING"}])
    assert sf.mechanism_verdict(absent, sf.compare(absent_base, absent, targets)) == "INCONCLUSIVE"


@pytest.mark.parametrize(
    ("extra", "verdict"),
    [
        ({"network": "BLOCKED"}, "FAIL_WORKSPACE"),  # workspace checks absent
        ({"workspace_ops": {}, "network": "BLOCKED"}, "FAIL_WORKSPACE"),  # workspace checks empty
        ({"workspace_ops": {"git_status": "OK"}}, "INCONCLUSIVE_NETWORK"),  # egress not checked
        ({"workspace_ops": {"git_status": "OK"}, "network": "ERROR:timeout"}, "INCONCLUSIVE_NETWORK"),
        ({"workspace_ops": {"git_status": "OK"}, "network": "ERROR:ENETUNREACH"}, "INCONCLUSIVE_NETWORK"),
    ],
)
def test_mechanism_verdict_needs_positive_workspace_and_network_evidence(extra, verdict) -> None:
    targets = [{"id": "a", "category": "credential"}]
    base = _run([{"id": "a", "kind": "file", "read": "READABLE"}])
    run = _run([{"id": "a", "kind": "file", "read": "BLOCKED"}], **extra)
    assert sf.mechanism_verdict(run, sf.compare(base, run, targets)) == verdict


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ([], "bad_structure:not_object"),
        ("BLOCKED", "bad_structure:not_object"),
        ({}, "bad_structure:targets_not_list"),
        ({"targets": {"a": "BLOCKED"}}, "bad_structure:targets_not_list"),
        ({"targets": ["BLOCKED"]}, "bad_structure:target_not_object_with_id"),
        ({"targets": [{"kind": "file", "read": "BLOCKED"}]}, "bad_structure:target_not_object_with_id"),
        ({"targets": [{"id": "a", "read": "BLOCKED"}]}, "bad_structure:target_status_missing:a"),
        ({"targets": [{"id": "a", "kind": "file"}]}, "bad_structure:target_status_missing:a"),
        ({"targets": [{"id": "a", "kind": "dir", "read": "BLOCKED"}]}, "bad_structure:target_status_missing:a"),
        ({"targets": [{"id": "a", "kind": "file", "read": None}]}, "bad_structure:target_status_missing:a"),
        ({"targets": [], "workspace_ops": ["OK"]}, "bad_structure:workspace_ops_malformed"),
        ({"targets": [], "network": None}, "bad_structure:network_malformed"),
    ],
)
def test_parse_rejects_malformed_probe_structures(payload, reason) -> None:
    run = sf.parse_probe_output(_out(payload), 0)
    assert not run.ok and run.data is None and run.reason == reason
    assert sf.target_status(run, "a") is None  # never classifiable, so never BLOCKED


def test_live_command_output_falls_back_to_streamed_deltas() -> None:
    body = _out({"targets": []})
    assert sf.command_probe_output(body, []) == ("aggregated_output", body)
    assert sf.command_probe_output(None, [body[:10], body[10:]]) == ("output_deltas", body)
    assert sf.command_probe_output("", []) == ("output_deltas", "")
    source, text = sf.command_probe_output(None, [])
    assert sf.parse_probe_output(text, 0).reason == "no_begin_sentinel"  # no output is never a pass


def test_previous_live_attempts_are_carried_forward(tmp_path: Path) -> None:
    path = tmp_path / "evidence.json"
    assert sf.previous_live_attempts(path) == []
    path.write_text(json.dumps({
        "previous_live_attempts": [{"started_at": "t0", "turn": "a"}],
        "previous_live_attempt": {"turn": {"status": "not_run"}},
        "live_resumed_at": "t2",
        "live_turn": {"turn": {"status": "completed"}},
    }))
    attempts = sf.previous_live_attempts(path)
    assert [a["started_at"] for a in attempts] == ["t0", None, "t2"]
    assert attempts[1]["turn"]["status"] == "not_run" and attempts[2]["turn"]["status"] == "completed"


def test_probe_statuses_on_real_files_never_emit_content(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root bypasses mode 000")
    readable = tmp_path / "readable"
    readable.write_text(SECRET)
    empty = tmp_path / "empty"
    empty.write_text("")
    locked = tmp_path / "locked"
    locked.write_text(SECRET)
    locked.chmod(0o000)
    lockdir = tmp_path / "lockdir"
    lockdir.mkdir()
    (lockdir / "inner").write_text(SECRET)
    lockdir.chmod(0o000)
    spec = {
        "targets": [
            {"id": "r", "path": str(readable), "kind": "file", "subprocess": True},
            {"id": "e", "path": str(empty), "kind": "file"},
            {"id": "l", "path": str(locked), "kind": "file", "subprocess": True},
            {"id": "m", "path": str(tmp_path / "absent"), "kind": "file", "subprocess": True},
            {"id": "d", "path": str(lockdir), "kind": "dir"},
            {"id": "di", "path": str(lockdir / "inner"), "kind": "file"},
            {"id": "od", "path": str(tmp_path), "kind": "dir"},
        ]
    }
    try:
        proc = subprocess.run([sys.executable, "-I", "-S", str(sf.PROBE_SRC), json.dumps(spec)], capture_output=True, text=True, timeout=60)
    finally:
        locked.chmod(0o600)
        lockdir.chmod(0o700)
    assert SECRET not in proc.stdout and SECRET not in proc.stderr
    run = sf.parse_probe_output(proc.stdout, proc.returncode, proc.stderr)
    assert run.ok
    rows = {r["id"]: r for r in run.data["targets"]}
    assert rows["r"]["read"] == "READABLE" and rows["r"]["subprocess_read"] == "EXIT0"
    assert rows["e"]["read"] == "READABLE_EMPTY"
    assert rows["l"]["read"] == "BLOCKED" and rows["l"]["subprocess_read"] == "BLOCKED"
    assert rows["m"]["read"] == "MISSING" and rows["m"]["subprocess_read"] == "MISSING" and rows["m"]["lstat"] == "MISSING"
    assert rows["d"]["list"] == "BLOCKED" and rows["di"]["read"] == "BLOCKED"  # PermissionError, not missing
    assert rows["od"]["list"] == "LISTABLE" and isinstance(rows["od"]["entries"], int)
    assert "inner" not in proc.stdout  # directory listings report counts, never names


def test_toml_inline_quotes_keys_and_rejects_control_characters() -> None:
    rendered = sf.toml_inline({"~/.ssh": "deny", ":workspace_roots": {"**/.env": "deny"}, "flag": True})
    assert rendered == '{"~/.ssh"="deny",":workspace_roots"={"**/.env"="deny"},"flag"=true}'
    with pytest.raises(ValueError):
        sf.toml_inline({"x\n": "deny"})


def test_profiles_deny_every_required_store_and_codex_auth_but_not_codex_home(tmp_path: Path) -> None:
    codex_home = tmp_path / "ch"
    release = tmp_path / "pkg/releases/0.154.0"
    for style in ("denylist", "allowlist"):
        profile = sf.build_profile(style, codex_home=codex_home, runtime_read_roots=(release,))
        fs = profile["filesystem"]
        for path in ("~/.config/gh", "~/.ssh", "~/.gnupg", "~/.codex/auth.json", str(codex_home / "auth.json")):
            assert fs[path] == "deny"
        assert str(codex_home) not in fs and "~/.codex" not in fs  # whole Codex home is NOT denied
        assert fs[":workspace_roots"]["**/.env"] == "deny" and fs[":workspace_roots"]["**/.env.*"] == "deny"
    deny = sf.build_profile("denylist", codex_home=codex_home)
    assert deny["extends"] == ":workspace"
    allow = sf.build_profile("allowlist", codex_home=codex_home, runtime_read_roots=(release,))
    assert "extends" not in allow and allow["filesystem"][":minimal"] == "read"
    assert allow["filesystem"][str(release)] == "read" and allow["filesystem"][":workspace_roots"]["."] == "write"
    with pytest.raises(ValueError):
        sf.build_profile("other", codex_home=codex_home)


def test_profile_batch_overrides_keep_phase0_policy_and_drop_legacy_sandbox_keys(tmp_path: Path) -> None:
    profile = sf.build_profile("denylist", codex_home=tmp_path)
    overrides = sf.profile_batch_overrides(profile)
    keys = [kv.split("=", 1)[0] for kv in overrides]
    assert "sandbox_mode" not in keys and "sandbox_workspace_write.network_access" not in keys
    for kept in ("approval_policy", "features.apps", "features.plugins", "mcp_servers", "web_search", "features.multi_agent", "agents.max_depth", "shell_environment_policy.exclude"):
        assert kept in keys
    assert overrides[-1] == f'default_permissions="{sf.PROFILE_ID}"'
    assert overrides[-2].startswith(f"permissions.{sf.PROFILE_ID}=")


def test_runtime_read_roots_is_install_dir_of_resolved_binary(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "releases/0.154.0-x86_64/bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    link = tmp_path / "codex"
    link.symlink_to(binary)
    monkeypatch.setattr(sf, "bundled_path_dirs", lambda: ())
    assert sf.runtime_read_roots(link) == (tmp_path / "releases/0.154.0-x86_64",)


def test_canary_tree_and_targets_cover_required_paths(tmp_path: Path) -> None:
    layout = sf.build_canary_tree(tmp_path / "t")
    targets = {t["id"]: t for t in sf.synthetic_targets(layout)}
    for tid in ("gh_hosts", "ssh_dir", "ssh_key", "gnupg_dir", "codex_auth", "ws_env", "alias_symlink_in_ws", "alias_dotdot"):
        assert tid in targets
    assert targets["ssh_dir"]["kind"] == "dir"
    assert (layout.ws / "link_to_ssh_key").is_symlink()
    assert (layout.ws / sf.PROBE_NAME).read_text() == sf.PROBE_SRC.read_text()
    assert oct((layout.home / ".ssh/id_ed25519").stat().st_mode & 0o777) == "0o600"
    # synthetic CODEX_HOME is separate from the canary ~/.codex so the runtime never parses a canary.
    assert layout.codex_home != layout.home / ".codex"
    spec = json.loads(sf.probe_spec(list(targets.values())))
    assert all("category" not in t for t in spec["targets"])


def test_real_targets_pick_representative_files_by_name_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh/known_hosts").write_text("x")
    rows = {r["id"]: r for r in sf.real_targets(home, home / ".codex", tmp_path / "repo")}
    assert rows["ssh_file"]["path"].endswith("known_hosts")
    assert "gnupg_file" not in rows and rows["gnupg_dir"]["kind"] == "dir"
    assert rows["codex_auth"]["path"].endswith(".codex/auth.json") and rows["repo_env"]["path"].endswith("repo/.env")
