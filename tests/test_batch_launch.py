"""Offline tests for the Phase 0 batch launch policy (no Codex or GitHub network)."""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spikes"))

from openai_codex import ApprovalMode, Codex, Sandbox  # noqa: E402
from openai_codex._approval_mode import _approval_mode_settings  # noqa: E402
from openai_codex._run import TurnResult  # noqa: E402
from openai_codex._sandbox import _sandbox_policy  # noqa: E402
from openai_codex.client import CodexClient, _params_dict  # noqa: E402
from openai_codex.generated.v2_all import TurnStartParams, TurnStatus  # noqa: E402

import batch_launch as bl  # noqa: E402

# Server->client request methods from codex-cli 0.154.0 `ServerRequest.json`.
SERVER_REQUEST_METHODS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
    "item/permissions/requestApproval",
    "item/tool/call",
    "account/chatgptAuthTokens/refresh",
    "attestation/generate",
    "applyPatchApproval",
    "execCommandApproval",
)


def _turn_result(final_response: str | None) -> TurnResult:
    return TurnResult(
        id="t", status=TurnStatus.completed, error=None, started_at=None, completed_at=None,
        duration_ms=None, final_response=final_response, items=[], usage=None,
    )


# --- approval mode -----------------------------------------------------------

def test_deny_all_maps_to_never_without_reviewer() -> None:
    policy, reviewer = _approval_mode_settings(ApprovalMode.deny_all)
    assert policy.root.value == "never"
    assert reviewer is None


def test_thread_and_turn_kwargs_always_carry_deny_all_and_explicit_sandbox() -> None:
    for sandbox in (Sandbox.read_only, Sandbox.workspace_write):
        for kwargs in (bl.thread_kwargs(sandbox), bl.turn_kwargs(sandbox)):
            assert kwargs["approval_mode"] is ApprovalMode.deny_all
            assert kwargs["sandbox"] is sandbox


def test_sdk_default_is_auto_review_so_we_must_never_rely_on_it() -> None:
    default = inspect.signature(Codex.thread_start).parameters["approval_mode"].default
    assert default is ApprovalMode.auto_review  # documents why explicit deny_all is mandatory


def test_sdk_low_level_default_handler_accepts_which_is_why_we_override() -> None:
    client = CodexClient()
    assert client._default_approval_handler("item/commandExecution/requestApproval", {}) == {"decision": "accept"}


def test_batch_codex_relies_on_known_private_attribute_names() -> None:
    src = inspect.getsource(Codex.__init__)
    assert "self._client = CodexClient(config=config)" in src
    assert "self._init = validate_initialize_metadata(self._client.initialize())" in src


def test_decline_replies_cover_every_approval_like_server_request() -> None:
    approval_like = [m for m in SERVER_REQUEST_METHODS if "pproval" in m or "elicitation" in m or "requestUserInput" in m]
    for method in approval_like:
        assert method in bl.DECLINE_REPLIES, method
    for reply in bl.DECLINE_REPLIES.values():
        assert reply.get("decision") not in ("accept", "acceptForSession", "approved", "approved_for_session")


def test_unknown_server_requests_fail_closed_and_known_ones_decline() -> None:
    codex = bl.BatchCodex.__new__(bl.BatchCodex)
    codex.server_requests = []
    assert codex._decline("item/commandExecution/requestApproval", {"command": "gh api user"}) == {"decision": "decline"}
    for unknown in ("item/tool/call", "account/chatgptAuthTokens/refresh", "something/new"):
        with pytest.raises(RuntimeError, match="unknown server request"):
            codex._decline(unknown, {})
    assert [r.method for r in codex.server_requests][:2] == ["item/commandExecution/requestApproval", "item/tool/call"]


def test_policy_kwargs_cannot_be_overridden_or_widened() -> None:
    with pytest.raises(ValueError, match="cannot be overridden"):
        bl.turn_kwargs(Sandbox.read_only, approval_mode=ApprovalMode.auto_review)
    with pytest.raises(ValueError, match="cannot be overridden"):
        bl.thread_kwargs(Sandbox.read_only, **{"approval_mode": ApprovalMode.auto_review})
    with pytest.raises(ValueError, match="read_only or"):
        bl.turn_kwargs(Sandbox.full_access)
    kw = bl.turn_kwargs(Sandbox.workspace_write, model="x")
    assert kw["approval_mode"] is ApprovalMode.deny_all and kw["sandbox"] is Sandbox.workspace_write


def test_extra_overrides_cannot_redefine_batch_keys(tmp_path: Path) -> None:
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n")
    with pytest.raises(ValueError, match="may not redefine"):
        bl.build_codex_config(tmp_path, codex_bin=fake_bin, extra_overrides=("sandbox_workspace_write.network_access=true",))
    with pytest.raises(ValueError, match="may not redefine"):
        bl.build_codex_config(tmp_path, codex_bin=fake_bin, extra_overrides=('approval_policy="on-request"',))


def test_require_wire_rejects_empty_or_partial_capture() -> None:
    with pytest.raises(RuntimeError, match="incomplete"):
        bl.require_wire([])
    with pytest.raises(RuntimeError, match="incomplete"):
        bl.require_wire([bl.WireRecord("thread/start", {})])
    bl.require_wire([bl.WireRecord("thread/start", {}), bl.WireRecord("turn/start", {})])


# --- sandbox / network on the wire ------------------------------------------

@pytest.mark.parametrize("sandbox,expected_type", [(Sandbox.read_only, "readOnly"), (Sandbox.workspace_write, "workspaceWrite")])
def test_turn_sandbox_policy_serializes_network_access_false(sandbox: Sandbox, expected_type: str) -> None:
    params = TurnStartParams(thread_id="x", input=[{"type": "text", "text": "hi"}], sandbox_policy=_sandbox_policy(sandbox))
    wire = _params_dict(params)
    assert wire["sandboxPolicy"]["type"] == expected_type
    assert wire["sandboxPolicy"]["networkAccess"] is False


def test_batch_config_overrides_disable_network_and_tool_paths() -> None:
    joined = "\n".join(bl.BATCH_CONFIG_OVERRIDES)
    for required in (
        'approval_policy="never"',
        "sandbox_workspace_write.network_access=false",
        "features.apps=false",
        "features.plugins=false",
        "features.remote_plugin=false",
        "mcp_servers={}",
        'web_search="disabled"',
        "tools.web_search=false",
        "agents.max_depth=0",
        "features.browser_use=false",
        "features.multi_agent=false",
    ):
        assert required in joined, required
    assert "network_access=true" not in joined
    assert "danger" not in joined


def test_launch_args_strip_tokens_and_start_app_server(tmp_path: Path) -> None:
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n")
    args = bl.build_launch_args(fake_bin)
    assert Path(args[0]).name == "env"
    for name in bl.GITHUB_TOKEN_ENV_VARS:
        idx = args.index(name)
        assert args[idx - 1] == "-u"
    assert args[-3:] == ["app-server", "--listen", "stdio://"]
    assert args.count("--config") == len(bl.BATCH_CONFIG_OVERRIDES)
    assert "GH_TOKEN" in bl.GITHUB_TOKEN_ENV_VARS and "GITHUB_TOKEN" in bl.GITHUB_TOKEN_ENV_VARS


def test_build_codex_config_uses_launch_override_and_cwd(tmp_path: Path) -> None:
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n")
    cfg = bl.build_codex_config(tmp_path, codex_bin=fake_bin)
    assert cfg.launch_args_override is not None
    assert str(fake_bin) in cfg.launch_args_override
    assert cfg.cwd == str(tmp_path)


# --- runtime / version gate -------------------------------------------------

def test_parse_version() -> None:
    assert bl.parse_version("codex-cli 0.154.0\n") == (0, 154, 0)
    assert bl.parse_version("nonsense") is None


def test_external_message_gate_rejects_old_runtime_and_missing_sdk_export() -> None:
    old = bl.RuntimeCheck("0.147.0", "/x", (0, 147, 0), sdk_external_message=True, runtime_meets_external_message_minimum=False)
    with pytest.raises(RuntimeError, match="0.147.0 <"):
        bl.require_runtime_for_external_message(old)
    no_sdk = bl.RuntimeCheck("0.147.0", "/x", (0, 154, 0), sdk_external_message=False, runtime_meets_external_message_minimum=True)
    with pytest.raises(RuntimeError, match="does not export ExternalMessage"):
        bl.require_runtime_for_external_message(no_sdk)
    ok = bl.RuntimeCheck("9.9.9", "/x", (0, 154, 0), sdk_external_message=True, runtime_meets_external_message_minimum=True)
    bl.require_runtime_for_external_message(ok)


def test_pinned_sdk_0_147_lacks_external_message() -> None:
    import openai_codex

    assert openai_codex.__version__ == "0.147.0"  # pinned in pyproject; revisit when a newer release ships
    assert not bl.sdk_has_external_message()


# --- structured output handling ---------------------------------------------

def test_parse_structured_handles_none_invalid_missing_and_ok() -> None:
    assert bl.parse_structured(_turn_result(None)).reason == "final_response_none"
    assert bl.parse_structured(_turn_result("not json")).reason.startswith("invalid_json")
    assert bl.parse_structured(_turn_result("[1]")).reason == "not_an_object"
    missing = bl.parse_structured(_turn_result('{"a": 1}'), required_keys=("a", "b"))
    assert not missing.ok and "b" in missing.reason
    ok = bl.parse_structured(_turn_result(json.dumps({"a": 1, "b": 2})), required_keys=("a", "b"))
    assert ok.ok and ok.data == {"a": 1, "b": 2}


def test_redact_masks_token_shapes() -> None:
    text = "token ghp_" + "a" * 36 + " and Bearer abcdefghijklmnopqrstuvwxyz"
    out = bl.redact(text)
    assert "ghp_" not in out and "Bearer abc" not in out
