"""Batch Codex launch configuration for the prflow Phase 0 spikes.

This module is deliberately small and self-contained. It wraps the published
``openai-codex`` SDK so that every batch thread and turn:

* uses ``ApprovalMode.deny_all`` explicitly (never the SDK default);
* passes an explicit ``Sandbox`` preset (``read_only`` or ``workspace_write``);
* runs the Codex runtime with process-level ``-c`` overrides that disable
  network access for sandboxed commands and disable app/plugin/MCP tool paths;
* strips GitHub token environment variables from the runtime's environment
  (via ``env -u``) so the child never inherits them;
* installs an approval handler that *declines and records* any server request
  (the SDK's own default handler would ACCEPT command/file approvals);
* records the exact JSON-RPC params sent for ``thread/start`` and
  ``turn/start`` so the wire-level policy can be shown as evidence.

Nothing here is product code; ``src/prflow/codex.py`` will be written in
Phase 2 using the conclusions from the spike document.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from openai_codex import (
    ApprovalMode,
    Codex,
    CodexConfig,
    Sandbox,
    TurnResult,
    __version__ as SDK_VERSION,
)
from openai_codex._initialize_metadata import validate_initialize_metadata
from openai_codex.client import CodexClient

JsonObject = dict[str, Any]

# Minimum runtime the spec (6.1) requires when ExternalMessage is needed.
MIN_RUNTIME_FOR_EXTERNAL_MESSAGE = (0, 151, 0)

# Obvious GitHub credential variables that must never reach the batch child.
GITHUB_TOKEN_ENV_VARS: tuple[str, ...] = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_PAT_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)

# Process-level config overrides passed as ``codex -c key=value``. Values are
# TOML. Keys were taken from the config schema embedded in codex-cli 0.154.0
# (see docs/spikes/0001-foundation.md, "Config keys used").
BATCH_CONFIG_OVERRIDES: tuple[str, ...] = (
    # Approval and sandbox defaults; the SDK also sets these per thread/turn.
    'approval_policy="never"',
    'sandbox_mode="read-only"',
    "sandbox_workspace_write.network_access=false",
    # No connector/plugin/MCP tool surfaces in batch mode.
    "features.apps=false",
    "features.plugins=false",
    "features.remote_plugin=false",
    "features.skill_mcp_dependency_install=false",
    "include_apps_instructions=false",
    "mcp_servers={}",
    # No model-side network tools either (web search runs server-side, outside the sandbox).
    'web_search="disabled"',
    "tools.web_search=false",
    "features.browser_use=false",
    "features.computer_use=false",
    "features.image_generation=false",
    "features.multi_agent=false",
    "agents.max_depth=0",
    # Defense in depth: hide token-like variables from sandboxed commands.
    'shell_environment_policy.exclude=["GH_TOKEN","GITHUB_TOKEN","GITHUB_PAT_TOKEN","GH_ENTERPRISE_TOKEN","GITHUB_ENTERPRISE_TOKEN","*_TOKEN","*_SECRET","*_API_KEY"]',
)

# Server -> client request methods known to codex-cli 0.154.0 (schema bundle
# ``ServerRequest.json``) and the declining reply for each. Any other method
# is treated as unknown and FAILS CLOSED: the handler raises, which makes the
# SDK reader thread fail every pending request/turn (``MessageRouter.fail_all``)
# so the caller sees an exception instead of a silently accepted request.
# The ``item/permissions/requestApproval`` reply shape (empty grant) has not
# been exercised live; ``request_permissions_tool`` is an off-by-default feature.
DECLINE_REPLIES: dict[str, JsonObject] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "execCommandApproval": {"decision": "denied"},
    "applyPatchApproval": {"decision": "denied"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "mcpServer/elicitation/request": {"action": "decline", "content": None},
    "item/tool/requestUserInput": {"answers": {}},
}


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def bundled_codex_bin() -> Path:
    from codex_cli_bin import bundled_codex_path, bundled_path_dir

    path = bundled_codex_path()
    _ = bundled_path_dir()
    return path


def bundled_path_dirs() -> tuple[Path, ...]:
    from codex_cli_bin import bundled_path_dir

    path_dir = bundled_path_dir()
    return (path_dir,) if path_dir is not None else ()


def resolve_codex_bin(codex_bin: str | os.PathLike[str] | None = None) -> Path:
    """Pick the runtime: explicit arg, ``PRFLOW_CODEX_BIN``, else the SDK's bundled binary."""
    candidate = codex_bin or os.environ.get("PRFLOW_CODEX_BIN")
    if candidate:
        path = Path(candidate).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Codex binary not found: {path}")
        return path
    return bundled_codex_bin()


def runtime_version(codex_bin: Path) -> tuple[int, int, int]:
    out = subprocess.run(
        [str(codex_bin), "--version"], check=True, capture_output=True, text=True, timeout=30
    ).stdout
    version = parse_version(out)
    if version is None:
        raise RuntimeError(f"could not parse runtime version from {out!r}")
    return version


def sdk_has_external_message() -> bool:
    import openai_codex

    return hasattr(openai_codex, "ExternalMessage")


@dataclass(slots=True)
class RuntimeCheck:
    sdk_version: str
    codex_bin: str
    runtime_version: tuple[int, int, int]
    sdk_external_message: bool
    runtime_meets_external_message_minimum: bool

    @property
    def batch_ready_for_external_message(self) -> bool:
        return self.sdk_external_message and self.runtime_meets_external_message_minimum

    def as_dict(self) -> JsonObject:
        return {
            "sdk_version": self.sdk_version,
            "codex_bin": self.codex_bin,
            "runtime_version": ".".join(map(str, self.runtime_version)),
            "sdk_external_message": self.sdk_external_message,
            "runtime_meets_external_message_minimum": self.runtime_meets_external_message_minimum,
            "batch_ready_for_external_message": self.batch_ready_for_external_message,
        }


def check_runtime(codex_bin: Path) -> RuntimeCheck:
    version = runtime_version(codex_bin)
    return RuntimeCheck(
        sdk_version=SDK_VERSION,
        codex_bin=str(codex_bin),
        runtime_version=version,
        sdk_external_message=sdk_has_external_message(),
        runtime_meets_external_message_minimum=version >= MIN_RUNTIME_FOR_EXTERNAL_MESSAGE,
    )


def require_runtime_for_external_message(check: RuntimeCheck) -> None:
    """Fail closed: refuse a runtime/SDK pair that cannot deliver ExternalMessage."""
    if not check.runtime_meets_external_message_minimum:
        raise RuntimeError(
            "batch mode unavailable: Codex runtime "
            f"{'.'.join(map(str, check.runtime_version))} < "
            f"{'.'.join(map(str, MIN_RUNTIME_FOR_EXTERNAL_MESSAGE))} required for ExternalMessage"
        )
    if not check.sdk_external_message:
        raise RuntimeError(
            f"batch mode unavailable: openai-codex {check.sdk_version} does not export ExternalMessage"
        )


def build_launch_args(
    codex_bin: Path,
    *,
    config_overrides: tuple[str, ...] = BATCH_CONFIG_OVERRIDES,
    strip_env: tuple[str, ...] = GITHUB_TOKEN_ENV_VARS,
) -> list[str]:
    """Full argv for the runtime. ``env -u`` removes token variables from the child."""
    env_bin = shutil.which("env")
    if env_bin is None:
        raise RuntimeError("`env` executable not found; cannot strip token variables")
    args: list[str] = [env_bin]
    for name in strip_env:
        args.extend(["-u", name])
    args.append(str(codex_bin))
    for kv in config_overrides:
        args.extend(["--config", kv])
    args.extend(["app-server", "--listen", "stdio://"])
    return args


def disable_overrides_for(mcp_server_names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """`mcp_servers={}` MERGES with inherited config (proven by spike_c_mcp_inherit.py), so every
    inherited server must be disabled by name. Names come from `config/read` of a first launch."""
    out: list[str] = []
    for name in mcp_server_names:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError(f"refusing to build override for MCP server name {name!r}")
        out.append(f"mcp_servers.{name}.enabled=false")
    return tuple(out)


def build_codex_config(
    cwd: Path,
    *,
    codex_bin: str | os.PathLike[str] | None = None,
    config_overrides: tuple[str, ...] = BATCH_CONFIG_OVERRIDES,
    extra_overrides: tuple[str, ...] = (),
) -> CodexConfig:
    resolved = resolve_codex_bin(codex_bin)
    batch_keys = {kv.split("=", 1)[0] for kv in config_overrides}
    clash = sorted(kv for kv in extra_overrides if kv.split("=", 1)[0] in batch_keys)
    if clash:
        raise ValueError(f"extra overrides may not redefine batch policy keys: {clash}")
    overrides = (*config_overrides, *extra_overrides)
    env: dict[str, str] = {}
    # The SDK only prepends its bundled PATH dir (rg) when it resolves the
    # binary itself; with launch_args_override we do it explicitly.
    path_dirs = bundled_path_dirs()
    if path_dirs:
        env["PATH"] = os.pathsep.join([*(str(p) for p in path_dirs), os.environ.get("PATH", "")])
    return CodexConfig(
        launch_args_override=tuple(build_launch_args(resolved, config_overrides=overrides)),
        cwd=str(cwd),
        env=env or None,
        client_name="prflow_spike",
        client_title="prflow Phase 0 spike",
    )


@dataclass(slots=True)
class WireRecord:
    method: str
    params: JsonObject
    response: JsonObject | None = None


@dataclass(slots=True)
class ServerRequestRecord:
    method: str
    summary: str
    reply: JsonObject


def _bounded_response(response: Any) -> JsonObject | None:
    """Keep only policy-relevant fields of thread/turn responses for evidence."""
    if hasattr(response, "model_dump"):
        dumped = response.model_dump(by_alias=True, exclude_none=True, mode="json")
    elif isinstance(response, dict):
        dumped = response
    else:
        return None
    keep = ("approvalPolicy", "approvalsReviewer", "sandbox", "model", "cwd", "reasoningEffort")
    out = {k: dumped[k] for k in keep if k in dumped}
    thread = dumped.get("thread")
    if isinstance(thread, dict):
        out["threadId"] = thread.get("id")
        out["cliVersion"] = thread.get("cliVersion")
    turn = dumped.get("turn")
    if isinstance(turn, dict):
        out["turnId"] = turn.get("id")
    return out


class BatchCodex(Codex):
    """``Codex`` client whose runtime is launched with the batch policy.

    The public ``Codex`` constructor gives no way to supply an approval handler,
    and ``CodexClient``'s default handler accepts approvals. This subclass
    replaces only the constructor so that all public thread/turn methods stay
    the SDK's own. The ``_client``/``_init`` attribute names come from
    ``openai_codex/api.py`` in openai-codex 0.147.0 and are checked by tests.
    """

    def __init__(self, config: CodexConfig) -> None:  # noqa: D107 - see class doc
        self.server_requests: list[ServerRequestRecord] = []
        self.wire: list[WireRecord] = []
        client = CodexClient(config=config, approval_handler=self._decline)
        original_request = client.request

        def recording_request(method: str, params: JsonObject | None = None, **kwargs: Any) -> Any:
            record: WireRecord | None = None
            if method in ("thread/start", "thread/resume", "turn/start"):
                record = WireRecord(method=method, params=json.loads(json.dumps(params or {})))
                self.wire.append(record)
            response = original_request(method, params, **kwargs)
            if record is not None:
                record.response = _bounded_response(response)
            return response

        client.request = recording_request  # type: ignore[method-assign]
        self._client = client
        try:
            self._client.start()
            self._init = validate_initialize_metadata(self._client.initialize())
        except Exception:
            self._client.close()
            raise

    def _decline(self, method: str, params: JsonObject | None) -> JsonObject:
        summary = json.dumps(params or {}, sort_keys=True)[:300]
        if method not in DECLINE_REPLIES:
            self.server_requests.append(ServerRequestRecord(method=method, summary=summary, reply={"error": "unknown server request; aborting"}))
            print(f"[batch_launch] UNKNOWN server request {method} -> abort", file=sys.stderr)
            raise RuntimeError(f"batch turn aborted: unknown server request {method}")
        reply = DECLINE_REPLIES[method]
        self.server_requests.append(ServerRequestRecord(method=method, summary=summary, reply=reply))
        print(f"[batch_launch] server request {method} -> declined", file=sys.stderr)
        return reply

    # Convenience for the spikes: generic JSON-RPC access for read-only evidence calls.
    def rpc(self, method: str, params: JsonObject | None = None) -> Any:
        return self._client._request_raw(method, params or {})

    def tool_server_inventory(self, thread_id: str | None = None) -> JsonObject:
        """MCP servers (with tool names) and apps visible to the runtime, optionally for one thread."""
        params: JsonObject = {"detail": "full"}
        if thread_id:
            params["threadId"] = thread_id
        mcp = self.rpc("mcpServerStatus/list", params).get("data", [])
        apps = self.rpc("app/list", {"threadId": thread_id} if thread_id else {}).get("data", [])
        return {
            "mcp_servers": [
                {
                    "name": s.get("name"),
                    "toolNames": sorted((s.get("tools") or {}).keys())[:50],
                    "runtimeStatus": s.get("runtimeStatus"),
                    "authStatus": s.get("authStatus"),
                }
                for s in mcp
            ],
            "apps": [{"id": a.get("id"), "name": a.get("name")} for a in apps],
        }

    def preflight_no_tool_servers(self, thread_id: str) -> JsonObject:
        """Fail closed (spec 5.6): a batch thread that can call any MCP tool or app is refused.

        A server entry that is disabled (no tools, not connected) is tolerated because
        `mcp_servers.<name>.enabled=false` leaves the config entry visible in status.
        """
        inventory = self.tool_server_inventory(thread_id)
        live = [s for s in inventory["mcp_servers"] if s["toolNames"] or s["runtimeStatus"] == "connected"]
        if live or inventory["apps"]:
            raise RuntimeError(f"batch preflight failed: tool servers exposed: {json.dumps(inventory)[:400]}")
        return inventory

    def stderr_tail(self, limit: int = 40) -> str:
        return self._client._stderr_tail(limit)


_LOCKED_KWARGS = ("approval_mode", "sandbox")


def _reject_locked(extra: JsonObject) -> None:
    clash = sorted(k for k in extra if k in _LOCKED_KWARGS)
    if clash:
        raise ValueError(f"batch policy keys cannot be overridden: {clash}")


def turn_kwargs(sandbox: Sandbox, **extra: Any) -> JsonObject:
    """Explicit per-turn policy: deny_all + explicit sandbox on every turn (not overridable)."""
    _reject_locked(extra)
    if not isinstance(sandbox, Sandbox) or sandbox is Sandbox.full_access:
        raise ValueError("batch turns must use Sandbox.read_only or Sandbox.workspace_write")
    return {**extra, "approval_mode": ApprovalMode.deny_all, "sandbox": sandbox}


def thread_kwargs(sandbox: Sandbox, **extra: Any) -> JsonObject:
    """Explicit per-thread policy: deny_all + explicit sandbox on every thread (not overridable)."""
    _reject_locked(extra)
    if not isinstance(sandbox, Sandbox) or sandbox is Sandbox.full_access:
        raise ValueError("batch threads must use Sandbox.read_only or Sandbox.workspace_write")
    return {"ephemeral": True, **extra, "approval_mode": ApprovalMode.deny_all, "sandbox": sandbox}


def require_wire(wire: list["WireRecord"]) -> None:
    """Checks written as all(...) over the wire log are vacuous on an empty log; refuse that."""
    methods = {w.method for w in wire}
    if "turn/start" not in methods or not ({"thread/start", "thread/resume"} & methods):
        raise RuntimeError(f"wire capture incomplete: {sorted(methods)}")


@dataclass(slots=True)
class StructuredOutcome:
    """Result of a turn whose contract requires JSON output."""

    ok: bool
    data: JsonObject | None
    reason: str | None
    final_response: str | None


def parse_structured(result: TurnResult, required_keys: tuple[str, ...] = ()) -> StructuredOutcome:
    """Handle ``final_response is None`` / invalid JSON / missing keys explicitly."""
    if result.final_response is None:
        return StructuredOutcome(False, None, "final_response_none", None)
    try:
        data = json.loads(result.final_response)
    except json.JSONDecodeError as exc:
        return StructuredOutcome(False, None, f"invalid_json: {exc.msg}", result.final_response)
    if not isinstance(data, dict):
        return StructuredOutcome(False, None, "not_an_object", result.final_response)
    missing = [key for key in required_keys if key not in data]
    if missing:
        return StructuredOutcome(False, data, f"missing_keys: {missing}", result.final_response)
    return StructuredOutcome(True, data, None, result.final_response)


def summarize_items(result: TurnResult, max_output: int = 400) -> list[JsonObject]:
    """Bounded, redacted view of thread items for evidence files."""
    rows: list[JsonObject] = []
    for item in result.items:
        root = item.root if hasattr(item, "root") else item
        row: JsonObject = {"type": type(root).__name__}
        for attr in ("command", "status", "exit_code", "phase", "server", "tool", "name"):
            if hasattr(root, attr):
                value = getattr(root, attr)
                row[attr] = value.value if hasattr(value, "value") else value
        for attr in ("aggregated_output", "text", "output"):
            if hasattr(root, attr):
                value = getattr(root, attr)
                if isinstance(value, str):
                    row[attr] = redact(value)[:max_output]
        rows.append(row)
    return rows


_TOKEN_RE = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{16,})")


def redact(text: str) -> str:
    return _TOKEN_RE.sub("<REDACTED>", text)


def evidence_path(name: str) -> Path:
    root = Path(__file__).resolve().parents[1] / "docs" / "spikes" / "evidence"
    root.mkdir(parents=True, exist_ok=True)
    return root / name


def launch_metadata() -> JsonObject:
    """Exact launch configuration that produced an evidence file (spec: record, don't assume)."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=repo_root()).stdout.strip() or None
    except OSError:
        commit = None
    return {
        "sdk_version": SDK_VERSION,
        "codex_bin_env": os.environ.get("PRFLOW_CODEX_BIN"),
        "config_overrides": list(BATCH_CONFIG_OVERRIDES),
        "stripped_env_vars": list(GITHUB_TOKEN_ENV_VARS),
        "git_commit": commit,
        "unknown_server_request_policy": "abort (handler raises)",
    }


def write_evidence(name: str, payload: JsonObject) -> Path:
    path = evidence_path(name)
    payload = {"launch": launch_metadata(), **payload}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def pick_model(codex: BatchCodex, preferred: tuple[str, ...]) -> str:
    """Choose the first *listed* model from ``preferred`` that the runtime reports.

    Never falls back to the account default silently: raise if none match.
    """
    listing = codex.models()
    slugs = [getattr(m, "model", None) or m.id for m in listing.data if not getattr(m, "hidden", False)]
    for want in preferred:
        if want in slugs:
            return want
    raise RuntimeError(f"none of {preferred} available; runtime lists {slugs}")


ProgressHook = Callable[[str], None]
