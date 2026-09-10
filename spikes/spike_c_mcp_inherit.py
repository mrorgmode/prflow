"""Spike C control: does the batch launch remove an *inherited* MCP server?

No model turns. Two positive controls inject a harmless local dummy MCP server
(spikes/dummy_mcp_server.py) through configuration only:

  1. a temporary CODEX_HOME whose config.toml defines the server (no auth files copied);
  2. a temporary repo-level .codex/config.toml in this trusted checkout (real CODEX_HOME).

For each: launch the app-server with the batch overrides MINUS `mcp_servers={}` and show
the dummy is configured/exposed (positive control), then launch with the full batch
overrides and show it is gone. If it is not gone, `BatchCodex.preflight_no_tool_servers`
must raise (fail closed). Temporary files are removed in `finally`.

    PRFLOW_CODEX_BIN=$(readlink -f "$(command -v codex)") uv run python spikes/spike_c_mcp_inherit.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openai_codex import Sandbox  # noqa: E402

from batch_launch import (  # noqa: E402
    BATCH_CONFIG_OVERRIDES,
    BatchCodex,
    disable_overrides_for,
    build_codex_config,
    redact,
    repo_root,
    resolve_codex_bin,
    thread_kwargs,
    write_evidence,
)

DUMMY = repo_root() / "spikes" / "dummy_mcp_server.py"
DUMMY_TOML = f'''[mcp_servers.prflow_dummy]
command = "{sys.executable}"
args = ["{DUMMY}"]
'''
WITHOUT_MCP_OVERRIDE = tuple(kv for kv in BATCH_CONFIG_OVERRIDES if kv != "mcp_servers={}")


def launch_and_inventory(codex_bin: Path, overrides: tuple[str, ...], env_extra: dict[str, str], label: str) -> dict[str, Any]:
    config = build_codex_config(repo_root(), codex_bin=codex_bin, config_overrides=overrides)
    config.env = {**(config.env or {}), **env_extra}
    out: dict[str, Any] = {"label": label, "mcp_servers_override_present": "mcp_servers={}" in overrides}
    try:
        with BatchCodex(config) as codex:
            cfg = codex.rpc("config/read", {"cwd": str(repo_root()), "includeLayers": False}).get("config", {})
            out["effective_mcp_servers_config"] = sorted((cfg.get("mcp_servers") or {}).keys())
            out["inventory_no_thread"] = codex.tool_server_inventory(None)
            thread = codex.thread_start(**thread_kwargs(Sandbox.read_only, cwd=str(repo_root()), model="gpt-5.6-luna"))
            out["inventory_thread"] = codex.tool_server_inventory(thread.id)
            try:
                codex.preflight_no_tool_servers(thread.id)
                out["preflight"] = "passed"
            except RuntimeError as exc:
                out["preflight"] = f"raised: {redact(str(exc))[:200]}"
            out["stderr_tail"] = [redact(l)[:160] for l in codex.stderr_tail(6).splitlines()]
    except Exception as exc:  # noqa: BLE001 - the control must report, not crash
        out["error"] = redact(str(exc))[:300]
    return out


def batch_with_disable(codex_bin: Path, env: dict[str, str], batch: dict[str, Any], label: str) -> dict[str, Any]:
    """Minimal fix: per-server `mcp_servers.<name>.enabled=false` for every inherited server name."""
    names = batch.get("effective_mcp_servers_config") or []
    extra = disable_overrides_for(names)
    out = launch_and_inventory(codex_bin, (*BATCH_CONFIG_OVERRIDES, *extra), env, label)
    out["disable_overrides"] = list(extra)
    return out


def control_temp_codex_home(codex_bin: Path) -> dict[str, Any]:
    home = Path(tempfile.mkdtemp(prefix="prflow-codex-home-"))
    try:
        (home / "config.toml").write_text(
            DUMMY_TOML + f'\n[projects."{repo_root()}"]\ntrust_level = "trusted"\n', encoding="utf-8"
        )
        env = {"CODEX_HOME": str(home)}
        batch = launch_and_inventory(codex_bin, BATCH_CONFIG_OVERRIDES, env, "temp-home/batch")
        return {
            "codex_home": "temporary (no auth files)",
            "positive_control": launch_and_inventory(codex_bin, WITHOUT_MCP_OVERRIDE, env, "temp-home/no-mcp-override"),
            "batch": batch,
            "batch_with_disable": batch_with_disable(codex_bin, env, batch, "temp-home/batch+disable"),
        }
    finally:
        shutil.rmtree(home, ignore_errors=True)


def control_repo_config(codex_bin: Path) -> dict[str, Any]:
    cfg_dir = repo_root() / ".codex"
    existed = cfg_dir.exists()
    cfg = cfg_dir / "config.toml"
    if existed and cfg.exists():
        return {"skipped": "repo .codex/config.toml already exists; not touching it"}
    try:
        cfg_dir.mkdir(exist_ok=True)
        cfg.write_text(DUMMY_TOML, encoding="utf-8")
        batch = launch_and_inventory(codex_bin, BATCH_CONFIG_OVERRIDES, {}, "repo-config/batch")
        return {
            "codex_home": "real (~/.codex), repo-level .codex/config.toml injected temporarily",
            "positive_control": launch_and_inventory(codex_bin, WITHOUT_MCP_OVERRIDE, {}, "repo-config/no-mcp-override"),
            "batch": batch,
            "batch_with_disable": batch_with_disable(codex_bin, {}, batch, "repo-config/batch+disable"),
        }
    finally:
        cfg.unlink(missing_ok=True)
        if not existed:
            shutil.rmtree(cfg_dir, ignore_errors=True)


def _exposed(run: dict[str, Any]) -> set[str]:
    """Servers that can actually be called: they list tools or report a live connection."""
    return {
        s["name"] for s in run.get("inventory_thread", {}).get("mcp_servers", [])
        if s.get("toolNames") or s.get("runtimeStatus") == "connected"
    }


def verdict(control: dict[str, Any]) -> dict[str, bool]:
    pos, bat, fixed = control.get("positive_control", {}), control.get("batch", {}), control.get("batch_with_disable", {})
    return {
        "positive_control_exposes_dummy": "prflow_dummy" in _exposed(pos),
        "mcp_servers_empty_override_removes_dummy": "prflow_dummy" in _exposed(pos) and not _exposed(bat),
        "batch_preflight_fails_closed_when_exposed": bat.get("preflight", "").startswith("raised") if _exposed(bat) else True,
        "per_server_disable_removes_dummy": "prflow_dummy" in _exposed(pos) and "batch_with_disable" in control and not _exposed(fixed) and fixed.get("preflight") == "passed",
    }


def main() -> int:
    codex_bin = resolve_codex_bin()
    evidence: dict[str, Any] = {"dummy_server": str(DUMMY.relative_to(repo_root()))}
    evidence["control_temp_codex_home"] = control_temp_codex_home(codex_bin)
    evidence["control_repo_config"] = control_repo_config(codex_bin)
    evidence["checks"] = {
        "temp_home": verdict(evidence["control_temp_codex_home"]),
        "repo_config": verdict(evidence["control_repo_config"]),
    }
    path = write_evidence("spike_c_mcp_inherit.json", evidence)
    print(json.dumps({"checks": evidence["checks"], "evidence": str(path)}, indent=2))
    ok = all(
        v["positive_control_exposes_dummy"] and v["batch_preflight_fails_closed_when_exposed"] and v["per_server_disable_removes_dummy"]
        for v in evidence["checks"].values()
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
