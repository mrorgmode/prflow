"""Raw rollout trace capture for Spike C, with guaranteed cleanup.

codex-cli 0.154.0 honours ``CODEX_ROLLOUT_TRACE_ROOT`` on the app-server process and
writes ``manifest.json``, ``trace.jsonl`` and ``payloads/N.json`` per thread. The
payloads include the exact inference request (tool definitions) and tool outputs,
but also model reasoning, so the directory is a temporary file that is always
removed in ``finally``; only sanitized metadata leaves :func:`analyze`.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Code-mode `exec` descriptions enumerate nested tools as "### `name`" headings.
NESTED_TOOL_RE = re.compile(r"^### `([A-Za-z0-9_]+)`", re.MULTILINE)


class TraceCapture:
    """Context manager: temporary trace root, deleted on exit whether or not the run succeeded."""

    def __init__(self) -> None:
        self.root: Path | None = None

    def __enter__(self) -> "TraceCapture":
        self.root = Path(tempfile.mkdtemp(prefix="prflow-trace-", dir="/tmp"))
        return self

    def env(self) -> dict[str, str]:
        assert self.root is not None
        return {"CODEX_ROLLOUT_TRACE_ROOT": str(self.root)}

    def __exit__(self, *_exc: object) -> None:
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)
            self.root = None


@dataclass(slots=True)
class TraceSummary:
    """Sanitized view: names and policy only, never message or reasoning text."""

    payload_files: int = 0
    event_types: list[str] = field(default_factory=list)
    thread_policy: dict[str, Any] | None = None
    top_level_tools: list[str] = field(default_factory=list)
    nested_tools_from_exec_description: list[str] = field(default_factory=list)
    runtime_all_tools: list[str] = field(default_factory=list)
    custom_tool_calls: list[str] = field(default_factory=list)

    @property
    def captured(self) -> bool:
        return bool(self.runtime_all_tools)

    def all_names(self) -> list[str]:
        return sorted(set(self.top_level_tools) | set(self.nested_tools_from_exec_description) | set(self.runtime_all_tools))

    def as_dict(self) -> dict[str, Any]:
        return {
            "payload_files": self.payload_files,
            "event_types": self.event_types,
            "thread_policy_from_runtime_trace": self.thread_policy,
            "top_level_tools": self.top_level_tools,
            "nested_tools_from_exec_description": self.nested_tools_from_exec_description,
            "runtime_ALL_TOOLS": self.runtime_all_tools,
            "custom_tool_calls": self.custom_tool_calls,
            "runtime_all_tools_captured": self.captured,
        }


def _iter(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter(v)


def _tool_names(tools: list[Any]) -> list[str]:
    names: list[str] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        inner = t.get("tools")
        if t.get("type") == "namespace" and isinstance(inner, list):
            names.extend(f"{t.get('name')}.{x.get('name')}" for x in inner if isinstance(x, dict))
        else:
            names.append(str(t.get("name") or (t.get("function") or {}).get("name") or t.get("type")))
    return names


def analyze_payloads(payloads: list[Any], events: list[dict[str, Any]] | None = None) -> TraceSummary:
    """Pure function over already-loaded payload objects (unit-tested)."""
    summary = TraceSummary(payload_files=len(payloads))
    if events:
        summary.event_types = sorted({str((e.get("payload") or {}).get("type") or e.get("type")) for e in events})
    top: set[str] = set()
    nested: set[str] = set()
    runtime: set[str] = set()
    calls: set[str] = set()
    for payload in payloads:
        if isinstance(payload, dict) and "sandbox_policy" in payload and summary.thread_policy is None:
            summary.thread_policy = {k: payload.get(k) for k in ("approval_policy", "sandbox_policy", "model")}
        for node in _iter(payload):
            tools = node.get("tools")
            if isinstance(tools, list) and node.get("type") in ("additional_tools", None) and tools and isinstance(tools[0], dict):
                top.update(_tool_names(tools))
            for value in node.values():
                if isinstance(value, str):
                    nested.update(NESTED_TOOL_RE.findall(value))
            if node.get("type") == "custom_tool_call" and node.get("name"):
                calls.add(str(node["name"]))
            if node.get("type") == "custom_tool_call_output":
                out = node.get("output")
                texts = [c.get("text", "") for c in out if isinstance(c, dict)] if isinstance(out, list) else [str(out)]
                for t in texts:
                    t = t.strip()
                    if t.startswith("ALL_TOOLS=") and "\n" not in t:
                        runtime.update(x for x in t[len("ALL_TOOLS="):].split(",") if x)
    summary.top_level_tools = sorted(top)
    summary.nested_tools_from_exec_description = sorted(nested)
    summary.runtime_all_tools = sorted(runtime)
    summary.custom_tool_calls = sorted(calls)
    return summary


def analyze(trace_root: Path) -> TraceSummary:
    events: list[dict[str, Any]] = []
    for p in trace_root.rglob("trace.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    payloads: list[Any] = []
    for p in sorted(trace_root.rglob("payloads/*.json")):
        try:
            payloads.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return analyze_payloads(payloads, events)


def surface_checks(names: list[str], captured: bool) -> dict[str, bool]:
    """Vacuity-safe: every negative check is False unless the runtime tool list was captured."""
    lowered = [n.lower() for n in names]
    return {
        "runtime_all_tools_captured": captured,
        "no_github_or_mcp_or_app_tools": captured and not any("github" in n or "codex_apps" in n or "mcp__" in n for n in lowered),
        "no_web_search_tool": captured and not any(n.startswith("web") for n in lowered),
        "no_multi_agent_tools": captured and not any("agent" in n for n in lowered),
    }


# JavaScript for the code-mode `exec` tool; its output is produced by the runtime, not the model.
ALL_TOOLS_JS = "text('ALL_TOOLS=' + ALL_TOOLS.map(t => t.name).sort().join(','))"
