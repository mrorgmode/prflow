"""Spike F.1: a batch permission profile that extends Codex's built-in ``:workspace``.

Amendment to Spike F (SPIKE_F.1.md). The candidate profile keeps ``:workspace`` (workspace
write, ``.git``/``.codex`` read-only) and narrows it: ``:root`` deny, ``:minimal`` read,
``:tmpdir``/``:slash_tmp`` deny (``:workspace`` would make them writable), the runtime
install directory read, credential-store and repository ``.env`` denies, and network
disabled. Git metadata that lives OUTSIDE the workspace root (a linked worktree's
administrative and common directories) gets a read-only exception derived from
``git rev-parse``; nothing else is added.

Both a normal checkout and a real linked worktree (``git worktree add``) are probed with
``codex sandbox`` and app-server ``command/exec`` (no model). Every write check has an
unsandboxed positive control on the same path, every read check a ``:workspace`` control.
All repositories, canaries and the Codex home are synthetic and removed afterwards.

    uv run python spikes/spike_f1_workspace_profile.py [--compare-bin PATH]

Nothing here is product code.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import spike_f_credential_read as sf  # noqa: E402
from batch_launch import disable_overrides_for, redact, runtime_version, write_evidence  # noqa: E402
from spike_f_probe import GIT_PATH_ARGS, GIT_PATH_KEYS  # noqa: E402

JsonObject = dict[str, Any]

EVIDENCE_NAME = "spike_f1_workspace_profile.json"
EXPECTED_ACTIVE = {"id": sf.PROFILE_ID, "extends": ":workspace"}
HOOK = "prflow-f1-existing"
INHERITED_MCP = "prflow_f1_inherited"  # synthetic inherited server: exercises the Phase 0 explicit disable
GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"}
BLOCKED_WRITE = frozenset({"BLOCKED", "READONLY"})
GUARDED = ("credential", "alias")


# --------------------------------------------------------------------------- profile


def git_metadata(checkout: Path) -> dict[str, str]:
    """Absolute metadata paths as Git itself resolves them (never assumed to be `<root>/.git`)."""
    proc = subprocess.run(["git", *GIT_PATH_ARGS], cwd=checkout, env=GIT_ENV, capture_output=True, text=True, timeout=30, check=True)
    lines = proc.stdout.splitlines()
    if len(lines) != len(GIT_PATH_KEYS):
        raise RuntimeError(f"unexpected rev-parse output: {len(lines)} lines")
    return dict(zip(GIT_PATH_KEYS, lines))


def git_metadata_read_roots(meta: dict[str, str], workspace_root: Path) -> tuple[Path, ...]:
    """Smallest read-only Git exception: the git dir and common dir, minus anything under the
    workspace root (``:workspace`` already makes that readable and keeps ``.git`` read-only),
    with a dir nested in another listed dir collapsed into it."""
    ws = workspace_root.resolve()
    dirs = sorted({Path(meta["git_dir"]).resolve(), Path(meta["common_dir"]).resolve()}, key=lambda p: (len(p.parts), str(p)))
    roots: list[Path] = []
    for d in dirs:
        if d == ws or ws in d.parents or any(r == d or r in d.parents for r in roots):
            continue
        roots.append(d)
    return tuple(roots)


def build_workspace_profile(*, codex_home: Path, runtime_read_roots: tuple[Path, ...], git_read_roots: tuple[Path, ...] = (), deny_tmp: bool = True) -> JsonObject:
    fs: JsonObject = {":root": "deny", ":minimal": "read"}
    if deny_tmp:
        fs.update({":tmpdir": "deny", ":slash_tmp": "deny"})
    fs.update({str(r): "read" for r in runtime_read_roots})
    fs.update({str(r): "read" for r in git_read_roots})
    fs[":workspace_roots"] = {pattern: "deny" for pattern in sf.WORKSPACE_GLOB_DENIES}
    fs.update(sf.credential_denies(codex_home))
    return {"description": "prflow Spike F.1 extends :workspace", "extends": ":workspace", "filesystem": fs, "network": {"enabled": False}}


def active_profile_ok(active: Any) -> bool:
    return isinstance(active, dict) and {k: active.get(k) for k in EXPECTED_ACTIVE} == EXPECTED_ACTIVE


# --------------------------------------------------------------------------- layout


@dataclass(slots=True)
class F1Layout:
    base: Path
    home: Path
    codex_home: Path
    checkouts: dict[str, Path]


def build_layout(base: Path) -> F1Layout:
    """Spike F canary tree (normal checkout `ws`) plus a real linked worktree `wt` of it."""
    lay = sf.build_canary_tree(base)
    wt = base / "wt"
    sf._git(lay.ws, "worktree", "add", "-q", "-b", "f1-linked", str(wt))
    for rel in (".env", "config/.env.production"):
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(sf.CANARY, encoding="utf-8")
        (wt / rel).chmod(0o600)
    (wt / "link_to_ssh_key").symlink_to(lay.home / ".ssh/id_ed25519")
    os.link(lay.home / ".config/gh/hosts.yml", wt / "hardlink_to_gh_hosts")
    shutil.copyfile(sf.PROBE_SRC, wt / sf.PROBE_NAME)
    checkouts = {"normal": lay.ws, "linked_worktree": wt}
    for co in checkouts.values():
        state = Path(git_metadata(co)["git_path_prflow"])
        state.mkdir()
        (state / "state.json").write_text('{"synthetic": true}\n', encoding="utf-8")
        (co / "README.md").write_text("# synthetic workspace\nuncommitted change\n", encoding="utf-8")  # non-empty diff
    hooks = Path(git_metadata(lay.ws)["common_dir"]) / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / HOOK).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (lay.codex_home / "config.toml").write_text(f'[mcp_servers.{INHERITED_MCP}]\ncommand = "/bin/false"\n', encoding="utf-8")
    return F1Layout(base, lay.home, lay.codex_home, checkouts)


def checkout_targets(lay: F1Layout, co: Path, meta: dict[str, str]) -> tuple[list[JsonObject], list[JsonObject]]:
    """Read targets (credentials, aliases, Git metadata) and write targets for one checkout."""
    creds = sf.synthetic_targets(sf.Layout(lay.base, lay.home, co, lay.codex_home))
    other = next(p for p in lay.checkouts.values() if p != co)
    gd, cd, state = Path(meta["git_dir"]), Path(meta["common_dir"]), Path(meta["git_path_prflow"])

    def t(tid: str, path: Path, category: str, kind: str = "file") -> JsonObject:
        return {"id": tid, "path": str(path), "category": category, "kind": kind, "subprocess": False}

    def w(wid: str, path: Path, op: str, category: str = "git_meta_write") -> JsonObject:
        return {"id": wid, "path": str(path), "op": op, "category": category}

    targets = [
        *creds,
        t("sibling_checkout_file", other / "README.md", "general"),
        t("meta_common_config", cd / "config", "git_meta"),
        t("meta_head", gd / "HEAD", "git_meta"),
        t("meta_index", gd / "index", "git_meta"),
        t("meta_prflow_state", state / "state.json", "git_meta"),
        t("meta_hook_existing", cd / "hooks" / HOOK, "git_meta"),
        t("meta_objects_dir", cd / "objects", "git_meta", "dir"),
        t("meta_refs_heads_dir", cd / "refs/heads", "git_meta", "dir"),
    ]
    writes = [
        w("ws_file_create", co / "prflow-f1-new-file", "create", "ws_write"),
        w("config_modify", cd / "config", "append"),
        w("head_modify", gd / "HEAD", "append"),
        w("index_modify", gd / "index", "append"),
        w("hooks_create", cd / "hooks/pre-commit", "create"),
        w("hooks_modify", cd / "hooks" / HOOK, "append"),
        w("prflow_state_modify", state / "state.json", "append"),
        w("prflow_state_create", state / "new-state.json", "create"),
        w("prflow_state_mkdir", state / "lock.d", "mkdir"),
        w("refs_create", cd / "refs/heads/prflow-f1-probe", "create"),
        w("objects_create", cd / "objects/prflow-f1-probe", "create"),
        w("slash_tmp_create", Path("/tmp") / f"prflow-f1-probe-{os.getpid()}", "create", "tmp_write"),
    ]
    if gd != cd:  # linked worktree: `.git` is a pointer file and HEAD differs per worktree
        targets += [t("meta_gitfile", co / ".git", "git_meta"), t("meta_common_head", cd / "HEAD", "git_meta")]
        writes += [w("gitfile_modify", co / ".git", "append"), w("common_head_modify", cd / "HEAD", "append")]
    return targets, writes


def f1_spec(targets: list[JsonObject], writes: list[JsonObject], *, network: bool = True) -> str:
    import json

    wire = [{k: v for k, v in t.items() if k != "category"} for t in targets]
    wwire = [{k: v for k, v in x.items() if k != "category"} for x in writes]
    return json.dumps({"targets": wire, "write_targets": wwire, "workspace_ops": True, "git": True, "network": network}, separators=(",", ":"))


# --------------------------------------------------------------------------- classification


def f1_structure_problem(data: JsonObject) -> str | None:
    writes = data.get("write_targets")
    if not isinstance(writes, list) or not all(isinstance(r, dict) and isinstance(r.get("id"), str) and isinstance(r.get("write"), str) for r in writes):
        return "write_targets_malformed"
    for key in ("git_ops", "git_write_ops"):
        value = data.get(key)
        if not isinstance(value, dict) or not value or not all(isinstance(v, str) for v in value.values()):
            return f"{key}_malformed"
    paths = data.get("git_paths")
    if paths is not None and not (isinstance(paths, dict) and all(isinstance(v, str) for v in paths.values())):
        return "git_paths_malformed"
    return None


def checked(run: sf.ProbeRun) -> sf.ProbeRun:
    """Spike F parsing plus the F.1 sections; a probe without them can never classify."""
    if run.data is not None:
        problem = f1_structure_problem(run.data)
        if problem is not None:
            return sf.ProbeRun(False, f"bad_structure:{problem}", None, run.exit_code, run.stderr_tail)
    return run


def write_status(run: sf.ProbeRun, write_id: str) -> str | None:
    if not run.ok or run.data is None:
        return None
    return next((r["write"] for r in run.data.get("write_targets", []) if r["id"] == write_id), None)


def compact_f1(run: sf.ProbeRun) -> JsonObject:
    out = run.compact()
    if run.data is not None:
        out["writes"] = {r["id"]: r["write"] for r in run.data.get("write_targets", [])}
        for key in ("git_ops", "git_write_ops", "git_paths"):
            if key in run.data:
                out[key] = run.data[key]
    return out


def f1_verdict(parent: sf.ProbeRun, baseline: sf.ProbeRun, run: sf.ProbeRun, targets: list[JsonObject], writes: list[JsonObject], expected_paths: dict[str, str]) -> JsonObject:
    """PASS needs every check positive; missing/unusable controls give INCONCLUSIVE, never PASS."""
    if not run.ok or run.data is None:
        return {"verdict": "BROKEN", "failures": [f"run:{run.reason}"], "inconclusive": []}
    fails: list[str] = []
    controls: list[str] = []
    rows = sf.compare(baseline, run, [t for t in targets if t["category"] in GUARDED])
    for row in rows:
        if row["verdict"] not in sf.STATUS_PROTECTED:
            (controls if row["verdict"].startswith("INCONCLUSIVE") else fails).append(f"credential:{row['id']}:{row['verdict']}")
    if not any(row["verdict"] in ("PROTECTED", "PROTECTED_MASKED") for row in rows):
        controls.append("no_present_credential_target")
    for t in targets:
        if t["category"] == "git_meta":
            status = sf.target_status(run, t["id"])
            if status not in ("READABLE", "LISTABLE"):
                fails.append(f"meta_read:{t['id']}:{status}")
    for w in writes:
        control, status = write_status(parent, w["id"]), write_status(run, w["id"])
        if control != "WRITABLE":
            controls.append(f"write_control:{w['id']}:{control}")
        ok = status == "WRITABLE" if w["category"] == "ws_write" else status in BLOCKED_WRITE
        if not ok:
            fails.append(f"write:{w['id']}:{status}")
    ops = run.data.get("workspace_ops") or {}
    if not ops:
        fails.append("workspace_ops_absent")
    fails += [f"workspace_op:{k}:{v}" for k, v in ops.items() if v != "OK"]
    fails += [f"git_op:{k}:{v}" for k, v in run.data["git_ops"].items() if v != "OK"]
    parent_writes = (parent.data or {}).get("git_write_ops") or {}
    for key, value in run.data["git_write_ops"].items():
        if value == "OK":
            fails.append(f"git_write:{key}:OK")
        if parent_writes.get(key) != "OK":
            controls.append(f"git_write_control:{key}:{parent_writes.get(key)}")
    if run.data.get("git_paths") != expected_paths:
        fails.append("git_paths_mismatch")
    network = run.data.get("network")
    if network == "CONNECTED":
        fails.append("network:CONNECTED")
    elif network != "BLOCKED":
        controls.append(f"network:{network}")
    verdict = "FAIL" if fails else "INCONCLUSIVE" if controls else "PASS"
    return {"verdict": verdict, "failures": fails, "inconclusive": controls}


# --------------------------------------------------------------------------- runners


def config_summary(cfg: JsonObject, profile: JsonObject) -> JsonObject:
    """What the runtime's layered config actually holds for the batch policy keys."""
    got = (cfg.get("permissions") or {}).get(sf.PROFILE_ID)
    return {
        "default_permissions": cfg.get("default_permissions"),
        "sandbox_mode": cfg.get("sandbox_mode"),
        "sandbox_workspace_write": cfg.get("sandbox_workspace_write"),
        "approval_policy": cfg.get("approval_policy"),
        "profile_present": got is not None,
        "profile_extends": (got or {}).get("extends"),
        "profile_network": (got or {}).get("network"),
        "profile_filesystem_keys_match": isinstance(got, dict) and set((got.get("filesystem") or {})) == set(profile["filesystem"]),
        "profile_filesystem_keys_missing": sorted(set(profile["filesystem"]) - set((got or {}).get("filesystem") or {})),
        "profile_filesystem_keys_extra": sorted(set((got or {}).get("filesystem") or {}) - set(profile["filesystem"])),
    }


def app_server_session(codex_bin: Path, co: Path, profile: JsonObject, env: dict[str, str], spec: str, targets: list[JsonObject], writes: list[JsonObject], parent: sf.ProbeRun, expected: dict[str, str]) -> JsonObject:
    overrides = sf.profile_batch_overrides(profile)
    out: JsonObject = {"launch_legacy_sandbox_keys": [kv.split("=", 1)[0] for kv in overrides if kv.split("=", 1)[0] in sf.LEGACY_SANDBOX_KEYS]}
    try:
        # Phase 0: inherited MCP names from a first launch, then relaunch with explicit disables.
        with sf.open_batch_codex(codex_bin, overrides, co, env) as codex:
            cfg = codex.rpc("config/read", {"cwd": str(co), "includeLayers": False}).get("config", {})
        names = sorted((cfg.get("mcp_servers") or {}).keys())
        disables = disable_overrides_for(names)
        out.update(inherited_mcp_servers=names, mcp_disable_overrides=list(disables), launch_overrides=[*overrides, *disables])
        with sf.open_batch_codex(codex_bin, (*overrides, *disables), co, env) as codex:
            cfg = codex.rpc("config/read", {"cwd": str(co), "includeLayers": False}).get("config", {})
            out["config_read"] = config_summary(cfg, profile)
            out["permission_profiles"] = sf.profile_list(codex, co)
            params = sf.sdk_thread_params(co, None)  # public SDK thread_start(sandbox=None) serialization
            res = codex.rpc("thread/start", params)
            out["thread_start"] = {"params_sandbox": params.get("sandbox"), "params_sandboxPolicy": params.get("sandboxPolicy"), **sf.effective_policy(res), "runtimeWorkspaceRoots": res.get("runtimeWorkspaceRoots")}
            out["active_profile_ok"] = active_profile_ok(res.get("activePermissionProfile"))
            try:
                codex.preflight_no_tool_servers(res["thread"]["id"])
                out["preflight"] = "passed"
            except RuntimeError as exc:
                out["preflight"] = f"raised: {redact(str(exc))[:200]}"
            base = checked(sf.exec_probe(codex, co, spec, permissionProfile=":workspace"))
            run = checked(sf.exec_probe(codex, co, spec))
            out["exec_positive_control_workspace"] = compact_f1(base)
            out["exec_config_default"] = {"run": compact_f1(run), **f1_verdict(parent, base, run, targets, writes, expected)}
    except Exception as exc:  # noqa: BLE001 - a refused launch is evidence
        out["error"] = redact(str(exc))[:400]
    return out


def sandbox_run(codex_bin: Path, profile: JsonObject, co: Path, spec: str, env: dict[str, str]) -> sf.ProbeRun:
    return checked(sf.run_codex_sandbox(codex_bin, sf.PROFILE_ID, (sf.profile_override(profile),), co, spec, env))


def run_runtime(codex_bin: Path, lay: F1Layout) -> JsonObject:
    # Only the selected runtime installation is required by these probes.
    roots = (codex_bin.resolve().parent.parent,)
    env = sf.minimal_env(lay.home, lay.codex_home)
    app_env = {"HOME": str(lay.home), "CODEX_HOME": str(lay.codex_home)}
    out: JsonObject = {"codex_bin": str(codex_bin), "codex_version": ".".join(map(str, runtime_version(codex_bin))), "runtime_read_roots": [str(r) for r in roots], "checkouts": {}}
    for name, co in lay.checkouts.items():
        meta = git_metadata(co)
        git_roots = git_metadata_read_roots(meta, co)
        targets, writes = checkout_targets(lay, co, meta)
        spec = f1_spec(targets, writes)
        profile = build_workspace_profile(codex_home=lay.codex_home, runtime_read_roots=roots, git_read_roots=git_roots)
        parent = checked(sf.run_parent(co, f1_spec(targets, writes, network=False), env))
        baseline = checked(sf.run_codex_sandbox(codex_bin, ":workspace", (), co, spec, env))
        prot = sandbox_run(codex_bin, profile, co, spec, env)
        entry: JsonObject = {
            "git_metadata": meta,
            "git_metadata_read_roots": [str(r) for r in git_roots],
            "profile": sf.profile_override(profile),
            "parent_unsandboxed": compact_f1(parent),
            "baseline_workspace": compact_f1(baseline),
            "codex_sandbox": {"run": compact_f1(prot), **f1_verdict(parent, baseline, prot, targets, writes, meta)},
        }
        # Necessity controls: is each added piece actually required?
        necessity: JsonObject = {}
        if git_roots:
            r = sandbox_run(codex_bin, build_workspace_profile(codex_home=lay.codex_home, runtime_read_roots=roots), co, spec, env)
            necessity["without_git_metadata_read_roots"] = {"ok": r.ok, "reason": r.reason, "git_ops": (r.data or {}).get("git_ops"), "meta_reads": {t["id"]: sf.target_status(r, t["id"]) for t in targets if t["category"] == "git_meta"}}
        if name == "normal":
            r = sandbox_run(codex_bin, build_workspace_profile(codex_home=lay.codex_home, runtime_read_roots=()), co, spec, env)
            necessity["without_runtime_read_roots"] = {"ok": r.ok, "reason": r.reason, "stderr_tail": r.stderr_tail}
            r = sandbox_run(codex_bin, build_workspace_profile(codex_home=lay.codex_home, runtime_read_roots=roots, deny_tmp=False), co, spec, env)
            necessity["without_tmp_denies"] = {"ok": r.ok, "slash_tmp_create": write_status(r, "slash_tmp_create")}
        entry["necessity_controls"] = necessity
        entry["app_server"] = app_server_session(codex_bin, co, profile, app_env, spec, targets, writes, parent, meta)
        out["checkouts"][name] = entry
    return out


def verdict_lines(label: str, rt: JsonObject) -> list[str]:
    lines = []
    for name, c in rt.get("checkouts", {}).items():
        app = c["app_server"]
        lines.append(
            f"{label} {rt['codex_version']} {name}: codex_sandbox={c['codex_sandbox']['verdict']} "
            f"app_server_exec={app.get('exec_config_default', {}).get('verdict', 'ERROR')} active_profile_ok={app.get('active_profile_ok')} "
            f"preflight={app.get('preflight')} git_read_roots={len(c['git_metadata_read_roots'])}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--compare-bin", help="second runtime binary for a deterministic cross-version check (no model)")
    args = parser.parse_args(argv)

    codex_bin = sf.resolve_codex_bin(os.environ.get("PRFLOW_CODEX_BIN") or shutil.which("codex"))
    real_home = Path.home()
    cache = real_home / ".cache"
    cache.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="prflow-spike-f1-", dir=cache))
    evidence: JsonObject = {"platform": sf.platform_info(codex_bin), "profile_id": sf.PROFILE_ID, "expected_active_profile": EXPECTED_ACTIVE, "runtimes": {}}
    replacements = {str(base): "<TMP>", str(real_home): "~"}
    try:
        lay = build_layout(base)
        evidence["runtimes"]["native"] = run_runtime(codex_bin, lay)
        if args.compare_bin:
            compare_bin = Path(args.compare_bin).resolve()
            replacements[str(compare_bin.parent.parent)] = "<COMPARE_RUNTIME_DIR>"
            try:
                evidence["runtimes"]["compare"] = run_runtime(compare_bin, lay)
            except Exception as exc:  # noqa: BLE001
                evidence["runtimes"]["compare"] = {"codex_bin": str(compare_bin), "error": redact(str(exc))[:400]}
    finally:
        shutil.rmtree(base, ignore_errors=True)
        evidence["temp_tree_removed"] = not base.exists()
    payload = sf.sanitize(evidence, replacements)
    path = write_evidence(EVIDENCE_NAME, payload)
    for label, rt in payload["runtimes"].items():
        for line in verdict_lines(label, rt) if "checkouts" in rt else [f"{label}: {rt.get('error')}"]:
            print(line, file=sys.stderr)
    print(f"evidence: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
