"""Offline runtime probe (no model turns, no Codex quota).

Starts the Codex app-server through the SDK with the batch launch policy and
records, per runtime:

* initialize metadata (server name/version);
* permission profiles, MCP server status (tools visible), apps list/installed,
  skills list, effective config keys of interest, model list;
* a `thread/start` with deny_all + read_only and the returned effective
  approvalPolicy / approvalsReviewer / sandbox;
* the same MCP/app inventory scoped to that thread.

Run against the bundled runtime (default) and the native one:

    uv run python spikes/probe_runtime.py
    PRFLOW_CODEX_BIN=$(readlink -f "$(command -v codex)") uv run python spikes/probe_runtime.py

Pass ``--baseline`` to also start a *default* (non-batch) app-server for
contrast. The baseline never runs a turn either.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox  # noqa: E402

from batch_launch import (  # noqa: E402
    BATCH_CONFIG_OVERRIDES,
    BatchCodex,
    build_codex_config,
    check_runtime,
    redact,
    repo_root,
    resolve_codex_bin,
    thread_kwargs,
    write_evidence,
)

# Cheapest *listed* model in the catalog on 2026-09-10 ("Fast and affordable
# agentic coding model"). Never the account default (gpt-6-astra).
PROBE_MODEL = "gpt-5.6-luna"

CONFIG_KEYS_OF_INTEREST = (
    "approval_policy",
    "sandbox_mode",
    "sandbox_workspace_write",
    "features",
    "mcp_servers",
    "shell_environment_policy",
    "include_apps_instructions",
    "tools",
    "web_search",
    "model",
    "default_permissions",
    "plugins",
)


def _dump(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, exclude_none=True, mode="json")
    return obj


def mcp_inventory(client: Any, thread_id: str | None) -> dict[str, Any]:
    params: dict[str, Any] = {"detail": "full"}
    if thread_id:
        params["threadId"] = thread_id
    try:
        raw = _dump(client.rpc("mcpServerStatus/list", params))
    except Exception as exc:  # noqa: BLE001 - evidence, not control flow
        return {"error": redact(str(exc))[:300]}
    servers = []
    for server in raw.get("data", []):
        tools = server.get("tools") or {}
        servers.append(
            {
                "name": server.get("name"),
                "pluginId": server.get("pluginId"),
                "authStatus": server.get("authStatus"),
                "runtimeStatus": server.get("runtimeStatus"),
                "toolCount": len(tools),
                "toolNames": sorted(tools)[:50],
            }
        )
    return {"serverCount": len(servers), "servers": servers}


def apps_inventory(client: Any, thread_id: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for method, params in (
        ("app/list", {"threadId": thread_id} if thread_id else {}),
        ("app/installed", {}),
    ):
        try:
            raw = _dump(client.rpc(method, params))
            entries = raw.get("data") or raw.get("apps") or []
            out[method] = [
                {
                    "id": e.get("id"),
                    "name": e.get("name"),
                    "isEnabled": e.get("isEnabled"),
                    "isAccessible": e.get("isAccessible"),
                }
                for e in entries
            ]
        except Exception as exc:  # noqa: BLE001
            out[method] = {"error": redact(str(exc))[:300]}
    return out


def config_view(client: Any) -> dict[str, Any]:
    try:
        raw = _dump(client.rpc("config/read", {"cwd": str(repo_root()), "includeLayers": False}))
    except Exception as exc:  # noqa: BLE001
        return {"error": redact(str(exc))[:300]}
    cfg = raw.get("config", raw)
    return {k: cfg.get(k) for k in CONFIG_KEYS_OF_INTEREST if k in cfg}


class PlainCodex(Codex):
    """SDK default launch (bundled runtime, user config, default handler) for contrast."""

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return self._client._request_raw(method, params or {})


def probe(codex: Any, label: str, *, start_thread: bool) -> dict[str, Any]:  # noqa: C901
    meta = codex.metadata
    report: dict[str, Any] = {
        "label": label,
        "initialize": {
            "serverName": meta.serverInfo.name if meta.serverInfo else None,
            "serverVersion": meta.serverInfo.version if meta.serverInfo else None,
            "userAgent": meta.userAgent,
        },
    }
    try:
        profiles = _dump(codex.rpc("permissionProfile/list", {"cwd": str(repo_root())}))
        report["permissionProfiles"] = profiles.get("data")
    except Exception as exc:  # noqa: BLE001
        report["permissionProfiles"] = {"error": redact(str(exc))[:300]}
    report["config"] = config_view(codex)
    try:
        skills = _dump(codex.rpc("skills/list", {"cwds": [str(repo_root())]}))
        names = []
        for entry in skills.get("data", []):
            for skill in entry.get("skills", []):
                names.append({"name": skill.get("name"), "path": skill.get("path"), "enabled": skill.get("enabled")})
        report["skills"] = names
    except Exception as exc:  # noqa: BLE001
        report["skills"] = {"error": redact(str(exc))[:300]}
    try:
        models = codex.models()
        report["models"] = [
            {"model": m.model, "hidden": m.hidden, "isDefault": m.is_default, "defaultEffort": _dump(m.default_reasoning_effort)}
            for m in models.data
        ]
    except Exception as exc:  # noqa: BLE001
        report["models"] = {"error": redact(str(exc))[:300]}
    report["mcp_no_thread"] = mcp_inventory(codex, None)
    report["apps_no_thread"] = apps_inventory(codex, None)

    if start_thread:
        thread = codex.thread_start(
            **thread_kwargs(
                Sandbox.read_only,
                cwd=str(repo_root()),
                model=PROBE_MODEL,
                developer_instructions="probe only; no turn will run",
            )
        )
        report["thread"] = {"id": thread.id}
        if hasattr(codex, "wire"):
            report["thread_start_wire"] = [
                {"method": w.method, "params": w.params, "response": w.response} for w in codex.wire if w.method == "thread/start"
            ]
        report["mcp_thread"] = mcp_inventory(codex, thread.id)
        report["apps_thread"] = apps_inventory(codex, thread.id)
    if hasattr(codex, "server_requests"):
        report["server_requests"] = [r.__dict__ for r in codex.server_requests]
    report["stderr_tail"] = [redact(line)[:200] for line in codex.stderr_tail(20).splitlines()] if hasattr(codex, "stderr_tail") else None
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="store_true", help="also probe an SDK-default app-server")
    parser.add_argument("--codex-bin", default=None)
    args = parser.parse_args()

    codex_bin = resolve_codex_bin(args.codex_bin)
    check = check_runtime(codex_bin)
    label = f"batch-{'.'.join(map(str, check.runtime_version))}"
    print(f"== {label}: {codex_bin}", file=sys.stderr)
    out: dict[str, Any] = {"runtime_check": check.as_dict(), "config_overrides": list(BATCH_CONFIG_OVERRIDES)}
    with BatchCodex(build_codex_config(repo_root(), codex_bin=codex_bin)) as codex:
        out["batch"] = probe(codex, label, start_thread=True)
    if args.baseline:
        print("== baseline (SDK default launch, no overrides)", file=sys.stderr)
        with PlainCodex(CodexConfig(cwd=str(repo_root()), codex_bin=str(codex_bin))) as plain:
            out["baseline"] = probe(plain, "baseline-sdk-default", start_thread=True)
    path = write_evidence(f"probe_runtime_{label}.json", out)
    print(json.dumps(out, indent=2, default=str)[:6000])
    print(f"\nevidence written: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
