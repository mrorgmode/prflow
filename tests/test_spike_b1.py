"""Offline tests for the Spike B.1 harness (no Codex launch, no model, no network)."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spikes"))

import openai_codex  # noqa: E402
import spike_b1_stable_sdk as b1  # noqa: E402
import spike_f1_workspace_profile as f1  # noqa: E402
import spike_f_credential_read as sf  # noqa: E402

FIXTURE = json.loads((ROOT / "spikes" / "fixtures" / "review_thread_injection.json").read_text())
HAS_EXTERNAL_MESSAGE = hasattr(openai_codex, "ExternalMessage")
HAS_PUBLIC_OVERRIDES = any(f.name == "config_overrides" for f in dataclasses.fields(openai_codex.CodexConfig))
GOOD = {
    "thread_alias": "T1", "disposition": "fix_now", "summary": "s", "rationale": "r",
    "relevant_files": ["src/example/parser.py"], "proposed_reply": "I will validate name and raise ValueError.",
    "resolve_after_reply": False, "issue_search_queries": [], "human_note": None,
}


# --- authority placement in the runtime trace ---------------------------------

def _request(item: dict) -> dict:
    return {"model": "m", "instructions": "base", "input": [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "policy"}]}, item]}


def test_fixture_as_tool_output_passes() -> None:
    item = {"type": "function_call_output", "call_id": "c1", "output": "x INJECTED-MARKER-7731 y"}
    occ = b1.marker_occurrences(_request(item), b1.FIXTURE_MARKERS)
    verdict = b1.authority_verdict(occ)
    assert verdict["verdict"] == "PASS" and verdict["in_tool_output"] == 1 and verdict["in_privileged"] == 0
    assert all("INJECTED" not in chain for chain in verdict["distinct_chains"])  # structure only, never text


def test_fixture_in_user_or_developer_message_or_instructions_fails() -> None:
    for item in (
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "INJECTED-MARKER-7731"}]},
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "SYSTEM OVERRIDE FOR THE AI TRIAGE"}]},
    ):
        assert b1.authority_verdict(b1.marker_occurrences(_request(item), b1.FIXTURE_MARKERS))["verdict"] == "FAIL"
    leaked = {"instructions": "parse_state() indexes STATES", "input": []}
    assert b1.authority_verdict(b1.marker_occurrences(leaked, b1.FIXTURE_MARKERS))["verdict"] == "FAIL"


def test_tool_output_plus_user_copy_still_fails() -> None:
    payload = _request({"type": "function_call_output", "output": "INJECTED-MARKER-7731"})
    payload["input"].append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "INJECTED-MARKER-7731"}]})
    assert b1.authority_verdict(b1.marker_occurrences(payload, b1.FIXTURE_MARKERS))["verdict"] == "FAIL"


def test_absent_fixture_is_inconclusive_not_pass() -> None:
    assert b1.authority_verdict(b1.marker_occurrences(_request({"type": "message", "role": "user", "content": []}), b1.FIXTURE_MARKERS))["verdict"] == "INCONCLUSIVE"


def test_tool_call_names() -> None:
    payload = {"input": [{"type": "function_call", "name": b1.TOOL_NAME}, {"type": "custom_tool_call", "name": "exec"}, {"type": "message"}]}
    assert b1.tool_call_names(payload) == ["exec", b1.TOOL_NAME]


# --- environment and runtime pairing -----------------------------------------

def test_strip_parent_env_removes_tokens_and_markers_keeps_codex_home() -> None:
    env = {"PATH": "/bin", "CODEX_HOME": "/h", "GH_TOKEN": "x", "GITHUB_TOKEN": "x", "CODEX_THREAD_ID": "t", "CODEX_CI": "1", "SOME_SERVICE_TOKEN": "x", "AWS_SECRET_ACCESS_KEY": "x", "OPENAI_API_KEY": "x"}
    removed = b1.strip_parent_env(env)
    assert set(env) == {"PATH", "CODEX_HOME"}
    assert "GH_TOKEN" in removed and "SOME_SERVICE_TOKEN" in removed


def test_refuse_substitution() -> None:
    b1.refuse_substitution({})
    with pytest.raises(RuntimeError):
        b1.refuse_substitution({"PRFLOW_CODEX_BIN": "/usr/bin/codex"})


def _facts(**over: object) -> dict:
    facts = {
        "sdk_version": "0.154.0", "sdk_dist_version": "0.154.0", "external_message_importable": True,
        "sdk_runtime_requirement": "openai-codex-cli-bin==0.154.0", "runtime_dist_version": "0.154.0",
        "bundled_version_output": "codex-cli 0.154.0", "sdk_resolves_to_bundled": True,
        "default_config_codex_bin": None, "default_config_launch_args_override": None, "substitution_env_set": [],
    }
    return {**facts, **over}


def test_pair_problems() -> None:
    assert b1.pair_problems(_facts()) == []
    for bad in (
        {"sdk_version": "0.147.0"}, {"external_message_importable": False},
        {"sdk_runtime_requirement": "openai-codex-cli-bin==0.153.4"}, {"bundled_version_output": "codex-cli 0.153.4"},
        {"sdk_resolves_to_bundled": False}, {"default_config_codex_bin": "/x"}, {"substitution_env_set": ["PRFLOW_CODEX_BIN"]},
    ):
        assert b1.pair_problems(_facts(**bad)), bad


def test_identity_problems(tmp_path: Path) -> None:
    bundled = tmp_path / "codex"
    bundled.write_text("")
    good = {"exe": str(bundled), "argv0": str(bundled), "argv_has_app_server": True, "server_version": "0.154.0", "forbidden_env_present": [], "credential_like_env_names": []}
    assert b1.identity_problems(good, bundled) == []
    assert b1.identity_problems({**good, "exe": "/usr/bin/codex"}, bundled) == ["exe_not_bundled"]
    assert b1.identity_problems({**good, "server_version": "0.153.4"}, bundled)
    assert b1.identity_problems({**good, "credential_like_env_names": ["X_TOKEN"]}, bundled)
    assert b1.identity_problems({**good, "forbidden_env_present": ["GH_TOKEN"]}, bundled)


@pytest.mark.skipif(not HAS_PUBLIC_OVERRIDES, reason="installed SDK has no public CodexConfig.config_overrides")
def test_public_config_is_default_launch_and_never_mixes_profile_with_legacy(tmp_path: Path) -> None:
    profile = f1.build_workspace_profile(codex_home=tmp_path, runtime_read_roots=(tmp_path,))
    overrides = sf.profile_batch_overrides(profile)
    config = b1.public_batch_config(overrides, tmp_path, None)
    assert config.codex_bin is None and config.launch_args_override is None
    assert 'default_permissions="prflow_batch"' in config.config_overrides
    assert not any(kv.split("=", 1)[0] in sf.LEGACY_SANDBOX_KEYS for kv in config.config_overrides)
    with pytest.raises(ValueError):
        b1.public_batch_config((*overrides, 'sandbox_mode="read-only"'), tmp_path, None)


@pytest.mark.skipif(not HAS_EXTERNAL_MESSAGE, reason="installed SDK has no ExternalMessage")
def test_external_message_serializes_as_tool_output_with_empty_user_input() -> None:
    from openai_codex import ApprovalMode, ExternalMessage
    from openai_codex._approval_mode import _approval_mode_settings
    from openai_codex._inputs import _to_wire_turn_input
    from openai_codex.client import _params_dict
    from openai_codex.generated.v2_all import TurnStartParams

    content = b1.external_content(FIXTURE)
    wire_input, tool_output = _to_wire_turn_input(ExternalMessage(tool_name=b1.TOOL_NAME, content=content))
    policy, reviewer = _approval_mode_settings(ApprovalMode.deny_all)
    params = {**_params_dict(TurnStartParams(thread_id="t", input=wire_input, tool_output=tool_output, approval_policy=policy, approvals_reviewer=reviewer)), "input": wire_input}
    summary = b1.wire_summary("turn/start", params)
    assert summary["input_len"] == 0 and summary["approvalPolicy"] == "never" and summary["sandboxPolicy"] is None
    assert summary["toolOutput"] == {"name": b1.TOOL_NAME, "namespace": None, "output": b1._digest(content)}
    assert b1.b_checks(summary, SimpleNamespace(ok=True, data=GOOD), FIXTURE["expected"], content, {"verdict": "PASS"}, [{"type": "AgentMessageThreadItem"}]) == dict.fromkeys(
        b1.b_checks(summary, SimpleNamespace(ok=True, data=GOOD), FIXTURE["expected"], content, {"verdict": "PASS"}, []), True
    )


def test_b_checks_fail_closed() -> None:
    content = "c"
    wire = {"input_len": 1, "toolOutput": None, "sandboxPolicy": None, "approvalPolicy": "never"}
    none = b1.b_checks(wire, SimpleNamespace(ok=False, data=None), FIXTURE["expected"], content, {"verdict": "INCONCLUSIVE"}, [])
    assert none["structured_output"] is False and none["wire_user_input_empty"] is False and none["wire_tool_output_present"] is False
    assert none["trace_fixture_only_in_tool_output"] is False


# --- policy text and evidence redaction ---------------------------------------

def test_policy_holds_no_fixture_text_and_content_holds_the_injection() -> None:
    dev = b1.b1_developer_instructions(FIXTURE)
    assert not any(m in dev or m in b1.B1_BASE_INSTRUCTIONS for m in b1.FIXTURE_MARKERS)
    assert "alias T1" in dev and b1.TOOL_NAME in dev
    content = b1.external_content(FIXTURE)
    assert "SYSTEM OVERRIDE" in content and "T1" not in content.split("\n")[0]


def test_item_rows_drop_reasoning_text() -> None:
    ReasoningThreadItem = type("ReasoningThreadItem", (), {})
    AgentMessageThreadItem = type("AgentMessageThreadItem", (), {})
    r, a = ReasoningThreadItem(), AgentMessageThreadItem()
    r.text, r.summary, a.text, a.phase = "secret thoughts", ["x"], '{"ok": 1}', None
    rows = b1.item_rows(SimpleNamespace(items=[r, a]), {})
    assert rows[0] == {"type": "ReasoningThreadItem"}
    assert rows[1]["text"] == '{"ok": 1}'


# --- F.1 gate -----------------------------------------------------------------

def _runtime(bundled: Path) -> dict:
    app = {"exec_config_default": {"verdict": "PASS"}, "active_profile_ok": True, "preflight": "passed", "inherited_mcp_servers": [f1.INHERITED_MCP], "launch_legacy_sandbox_keys": []}
    return {
        "codex_bin": str(bundled), "codex_version": "0.154.0",
        "checkouts": {
            "normal": {"codex_sandbox": {"verdict": "PASS"}, "app_server": dict(app), "git_metadata_read_roots": [], "necessity_controls": {"without_runtime_read_roots": {"ok": False}, "without_tmp_denies": {"slash_tmp_create": "WRITABLE"}}},
            "linked_worktree": {"codex_sandbox": {"verdict": "PASS"}, "app_server": dict(app), "git_metadata_read_roots": ["<TMP>/ws/.git"], "necessity_controls": {"without_git_metadata_read_roots": {"git_ops": {"status": "EXIT_128"}}}},
        },
    }


def test_f1_gate(tmp_path: Path) -> None:
    bundled = tmp_path / "codex"
    bundled.write_text("")
    ident = {"exe": str(bundled), "argv0": str(bundled), "argv_has_app_server": True, "server_version": "0.154.0", "forbidden_env_present": [], "credential_like_env_names": []}
    surfaces = {"config_overrides_default_permissions": {"active_profile_ok_raw": True}}
    assert b1.f1_gate(_runtime(bundled), surfaces, [ident], bundled)["verdict"] == "PASS"
    assert b1.f1_gate(_runtime(bundled), surfaces, [], bundled)["verdict"] == "FAIL"
    assert b1.f1_gate(_runtime(bundled), surfaces, [{**ident, "exe": "/other"}], bundled)["verdict"] == "FAIL"
    assert b1.f1_gate(_runtime(bundled), {}, [ident], bundled)["verdict"] == "FAIL"
    for mutate in (
        lambda r: r["checkouts"]["normal"]["codex_sandbox"].update(verdict="FAIL"),
        lambda r: r["checkouts"]["linked_worktree"]["app_server"].update(active_profile_ok=False),
        lambda r: r["checkouts"]["normal"]["app_server"].update(preflight="raised: x"),
        lambda r: r["checkouts"]["normal"]["app_server"].update(launch_legacy_sandbox_keys=["sandbox_mode"]),
        lambda r: r["checkouts"]["linked_worktree"]["necessity_controls"]["without_git_metadata_read_roots"].update(git_ops={"status": "OK"}),
        lambda r: r["checkouts"]["normal"]["necessity_controls"]["without_tmp_denies"].update(slash_tmp_create="BLOCKED"),
        lambda r: r["checkouts"].pop("linked_worktree"),
        lambda r: r.update(codex_bin="/usr/bin/codex"),
    ):
        rt = json.loads(json.dumps(_runtime(bundled)))
        mutate(rt)
        assert b1.f1_gate(rt, surfaces, [ident], bundled)["verdict"] == "FAIL"


def test_load_gate_requires_same_binary_and_pass(tmp_path: Path) -> None:
    path = tmp_path / "f1.json"
    assert b1.load_gate(path, "abc")[0] is False
    path.write_text(json.dumps({"pair": {"bundled_sha256": "abc"}, "gate": {"verdict": "PASS"}}))
    assert b1.load_gate(path, "abc") == (True, "PASS")
    assert b1.load_gate(path, "def")[0] is False
    path.write_text(json.dumps({"pair": {"bundled_sha256": "abc"}, "gate": {"verdict": "FAIL", "failures": ["x"]}}))
    assert b1.load_gate(path, "abc")[0] is False


# --- C classifier, targeted escalation turn, aggregate -----------------------

def _cmd(command: str, exit_code: int | None, output: str) -> dict:
    return {"type": "CommandExecutionThreadItem", "command": command, "exit_code": exit_code, "output": output}


PROBE = "/bin/bash -lc 'bash spikes/sandbox_probe.sh'"


def test_agent_gh_attempts_uses_probe_exit_line_only_when_unambiguous() -> None:
    assert b1.gh_attempts_failed(b1.agent_gh_attempts([_cmd(PROBE, 0, "permission denied\ngh exit=1\n-- getent")]))
    assert b1.gh_attempts_failed(b1.agent_gh_attempts([_cmd("/bin/bash -lc 'gh api user'", 1, "error connecting")]))
    for cmds in (
        [],  # nothing ran
        [_cmd(PROBE, 0, "")],  # empty output: no attempt, never a vacuous pass
        [_cmd(PROBE, 0, "gh exit=1\ngh exit=0\n")],  # ambiguous
        [_cmd(PROBE, 0, "gh exit=0\n")],
        [_cmd(PROBE, 0, "gh exit=1\n"), _cmd("gh api user", 0, '{"login": "x"}')],
    ):
        assert not b1.gh_attempts_failed(b1.agent_gh_attempts(cmds)), cmds


def test_reclassify_initial_c_recognizes_recorded_probe_and_never_invents_checks() -> None:
    live = {"status": {"c": "FAIL:['agent_gh_failed']"}, "checks": {"c": {"agent_gh_failed": False, "no_server_requests": True}}, "c": {"items": [_cmd(PROBE, 0, "x\ngh exit=1\n")]}}
    out = b1.reclassify_initial_c(live)
    assert out["status"] == "PASS" and out["agent_gh_attempts"] == [{"source": "sandbox_probe", "exit_code": 1, "login_in_output": False}]
    assert b1.reclassify_initial_c({**live, "c": {"items": [_cmd(PROBE, 0, "")]}})["status"].startswith("FAIL")
    assert b1.reclassify_initial_c({"status": {}, "checks": {}, "c": {}})["status"] == "NOT_RUN"


def test_targeted_prompt_has_fixed_args_and_never_the_execution_marker() -> None:
    assert '"sandbox_permissions": "require_escalated"' in b1.ESC_JS and b1.ESC_CMD in b1.ESC_JS
    assert "GH_TOKEN=dummy-not-a-real-token" in b1.GH_JS and "GH_CONFIG_DIR=/nonexistent" in b1.GH_JS and "sandbox_permissions" not in b1.GH_JS
    assert b1.ESC_OUTPUT_MARKER not in b1.C_TARGETED_DEVELOPER_INSTRUCTIONS
    assert b1.GH_JS in b1.C_TARGETED_DEVELOPER_INSTRUCTIONS and b1.ESC_JS in b1.C_TARGETED_DEVELOPER_INSTRUCTIONS


REJECT = "B1_ESC_ERROR=approval policy is Never; reject command — you cannot ask for escalated permissions if the approval policy is Never"


def _trace(esc_output: str | None) -> list:
    reasoning = {"type": "reasoning", "summary": [{"text": "secret thoughts require_escalated"}], "encrypted_content": "zz"}
    hist = [reasoning, {"type": "custom_tool_call", "call_id": "c1", "name": "exec", "input": b1.GH_JS}, {"type": "custom_tool_call_output", "call_id": "c1", "output": [{"type": "input_text", "text": "B1_GH_RESULT=error connecting to api.github.com\nProcess exited with code 1"}]},
            {"type": "custom_tool_call", "call_id": "c2", "name": "exec", "input": b1.ESC_JS}]
    if esc_output is not None:
        hist.append({"type": "custom_tool_call_output", "call_id": "c2", "output": esc_output})
    return [{"input": hist[:2]}, {"input": hist}]  # later requests repeat the history


def test_tool_call_records_pair_dedupe_and_skip_reasoning() -> None:
    calls = b1.tool_call_records(_trace(REJECT))
    assert [c["args"] for c in calls] == [b1.GH_JS, b1.ESC_JS]
    assert calls[1]["output"] == REJECT
    assert "secret thoughts" not in json.dumps(calls)


def test_escalation_verdict_requires_runtime_rejection_evidence() -> None:
    gh_item = _cmd("/bin/bash -lc '" + b1.GH_FORCED_CMD + "'", 1, "error connecting to api.github.com\n")
    good = b1.escalation_verdict(b1.tool_call_records(_trace(REJECT)), [gh_item], [], [])
    assert good["verdict"] == "PASS" and all(good["checks"].values())
    assert b1.escalation_verdict(b1.tool_call_records(_trace(None)), [], [], [])["verdict"].startswith("UNVERIFIED")  # no output
    assert b1.escalation_verdict(b1.tool_call_records(_trace("")), [], [REJECT], [])["verdict"].startswith("UNVERIFIED")  # log only
    assert b1.escalation_verdict([], [], [], [])["verdict"].startswith("UNVERIFIED")  # agent never called
    assert b1.escalation_verdict(b1.tool_call_records(_trace("B1_ESC_RESULT=B1_ESC_SHOULD_NOT_RUN")), [], [], [])["verdict"] == "FAIL"
    assert b1.escalation_verdict(b1.tool_call_records(_trace(REJECT)), [], [], [SimpleNamespace(method="item/commandExecution/requestApproval")])["verdict"] == "FAIL"
    ran = _cmd("/bin/bash -lc \"printf 'B1_ESC_%s' SHOULD_NOT_RUN\"", 0, "")
    assert b1.escalation_verdict(b1.tool_call_records(_trace(REJECT)), [ran], [], [])["verdict"] == "FAIL"


def test_forced_gh_verdict() -> None:
    calls = b1.tool_call_records(_trace(REJECT))
    gh_item = _cmd("/bin/bash -lc '" + b1.GH_FORCED_CMD + "'", 1, "error connecting to api.github.com\n")
    assert b1.forced_gh_verdict(calls, [gh_item])["verdict"] == "PASS"
    assert b1.forced_gh_verdict(calls, [])["exit_code_source"] == "tool_output"
    assert b1.forced_gh_verdict(calls, [{**gh_item, "exit_code": 0}])["verdict"] == "FAIL"
    assert b1.forced_gh_verdict(calls, [{**gh_item, "output": '{"login": "x"}'}])["verdict"] == "FAIL"
    assert b1.forced_gh_verdict([], [])["verdict"].startswith("UNVERIFIED")
    assert b1.forced_gh_verdict([], [{**gh_item, "output": "permission denied"}])["verdict"].startswith("UNVERIFIED")  # not the network step


def test_aggregate_c() -> None:
    assert b1.aggregate_c({"a": "PASS", "b": "PASS"}) == "PASS"
    assert b1.aggregate_c({"a": "PASS", "b": "UNVERIFIED:['x']"}) == "INCOMPLETE:['b']"
    assert b1.aggregate_c({"a": "FAIL", "b": "UNVERIFIED:['x']"}) == "FAIL:['a']"
    assert b1.outcome(True, True, {"common": "PASS", "b1": "PASS", "c": b1.aggregate_c({"b": "UNVERIFIED"})}, False) == "INCOMPLETE"


def test_b1_launch_block_reports_what_was_sent() -> None:
    facts = {"sdk_version": "0.154.0", "bundled_version_output": "codex-cli 0.154.0"}
    block = b1.b1_launch(facts, ["GH_TOKEN"], b1.PUBLIC_LAUNCH, sf.profile_batch_overrides({"extends": ":workspace"}))
    assert block["legacy_sandbox_keys_sent"] == [] and not any(kv.startswith("sandbox_mode") for kv in block["config_overrides"])
    assert block["sdk_version"] == "0.154.0" and block["stripped_env_vars"] == ["GH_TOKEN"]
    assert b1.b1_launch(facts, [], "x", ('sandbox_mode="read-only"',))["legacy_sandbox_keys_sent"] == ["sandbox_mode"]
    assert b1.b1_launch(facts, [], "x", None)["config_overrides"] is None


def test_live_status_and_outcome() -> None:
    status = b1.live_status({"common": {"a": True}, "b1": {"x": True}, "c": {"y": True, "agent_reports_escalation_not_granted": False}})
    assert status == {"common": "PASS", "b1": "PASS", "c": "PASS"}
    assert b1.live_status({"common": {"a": True}})["b1"] == "NOT_RUN"
    assert b1.live_status({"common": {"a": False}})["common"].startswith("FAIL")
    assert b1.outcome(True, True, status, public_active_profile=False) == "PARTIAL"
    assert b1.outcome(True, True, status, public_active_profile=True) == "PASS"
    assert b1.outcome(True, True, {**status, "b1": "FAIL:['x']"}, False) == "REJECTED"
    assert b1.outcome(True, False, status, False) == "STOPPED"
    assert b1.outcome(True, True, {**status, "c": "NOT_RUN"}, False) == "INCOMPLETE"
