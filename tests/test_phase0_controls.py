"""Offline tests for the resumed Phase 0 controls: trace analysis, MCP disable, preflight, dummy server."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spikes"))

import batch_launch as bl  # noqa: E402
import rollout_trace as rt  # noqa: E402


def _exec_description() -> str:
    return "Run JS...\n### `exec_command`\n...\n### `apply_patch`\n...\n"


def _payloads(with_all_tools: bool, names: str = "apply_patch,exec_command,view_image") -> list:
    request = {"input": [{"type": "additional_tools", "role": "developer", "tools": [
        {"type": "namespace", "name": "functions", "tools": [{"type": "custom", "name": "exec", "description": _exec_description()}]}]}]}
    thread = {"approval_policy": "never", "sandbox_policy": "WorkspaceWrite { network_access: false }", "model": "m"}
    out = [thread, request, {"input": [{"type": "custom_tool_call", "name": "exec", "input": rt.ALL_TOOLS_JS}]}]
    if with_all_tools:
        out.append({"input": [{"type": "custom_tool_call_output", "output": [{"type": "input_text", "text": f"ALL_TOOLS={names}"}]}]})
    return out


def test_trace_analysis_extracts_names_only_and_flags_capture() -> None:
    s = rt.analyze_payloads(_payloads(True))
    assert s.captured
    assert s.runtime_all_tools == ["apply_patch", "exec_command", "view_image"]
    assert s.nested_tools_from_exec_description == ["apply_patch", "exec_command"]
    assert s.top_level_tools == ["functions.exec"]
    assert s.thread_policy["approval_policy"] == "never"
    assert s.custom_tool_calls == ["exec"]
    assert "Run JS" not in json.dumps(s.as_dict())  # description/reasoning text never leaves analysis


def test_surface_checks_are_not_vacuous_without_runtime_capture() -> None:
    s = rt.analyze_payloads(_payloads(False))
    assert not s.captured
    assert not any(rt.surface_checks(s.all_names(), s.captured).values())
    good = rt.surface_checks(["apply_patch", "exec_command"], True)
    assert all(good.values())
    bad = rt.surface_checks(["exec_command", "mcp__codex_apps__github_create_issue"], True)
    assert not bad["no_github_or_mcp_or_app_tools"]
    assert not rt.surface_checks(["web__run"], True)["no_web_search_tool"]
    assert not rt.surface_checks(["multi_agent_v1__spawn_agent"], True)["no_multi_agent_tools"]


def test_trace_capture_cleans_up_even_on_error() -> None:
    with pytest.raises(RuntimeError):
        with rt.TraceCapture() as trace:
            root = trace.root
            assert root is not None and root.exists()
            (root / "trace.jsonl").write_text("{}")
            raise RuntimeError("boom")
    assert not root.exists()


def test_disable_overrides_for_inherited_servers_and_name_validation() -> None:
    assert bl.disable_overrides_for(["prflow_dummy", "other-1"]) == (
        "mcp_servers.prflow_dummy.enabled=false", "mcp_servers.other-1.enabled=false")
    with pytest.raises(ValueError):
        bl.disable_overrides_for(['x"y'])


def test_preflight_fails_closed_on_live_server_or_app_but_tolerates_disabled(monkeypatch) -> None:
    codex = bl.BatchCodex.__new__(bl.BatchCodex)
    cases = {
        "live": ({"mcp_servers": [{"name": "d", "toolNames": ["t"], "runtimeStatus": "connected", "authStatus": None}], "apps": []}, True),
        "connected_no_tools": ({"mcp_servers": [{"name": "d", "toolNames": [], "runtimeStatus": "connected", "authStatus": None}], "apps": []}, True),
        "app": ({"mcp_servers": [], "apps": [{"id": "connector_x", "name": "GitHub"}]}, True),
        "disabled": ({"mcp_servers": [{"name": "d", "toolNames": [], "runtimeStatus": "disabled", "authStatus": None}], "apps": []}, False),
        "empty": ({"mcp_servers": [], "apps": []}, False),
    }
    for label, (inventory, should_raise) in cases.items():
        monkeypatch.setattr(bl.BatchCodex, "tool_server_inventory", lambda self, thread_id=None, inv=inventory: inv)
        if should_raise:
            with pytest.raises(RuntimeError, match="preflight failed"):
                codex.preflight_no_tool_servers("t")
        else:
            assert codex.preflight_no_tool_servers("t") == inventory, label


def test_dummy_mcp_server_speaks_initialize_and_tools_list() -> None:
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.run([sys.executable, str(ROOT / "spikes" / "dummy_mcp_server.py")],
                          input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, timeout=20)
    replies = [json.loads(l) for l in proc.stdout.splitlines()]
    assert replies[0]["result"]["serverInfo"]["name"] == "prflow-dummy"
    assert [t["name"] for t in replies[1]["result"]["tools"]] == ["prflow_dummy_echo"]
