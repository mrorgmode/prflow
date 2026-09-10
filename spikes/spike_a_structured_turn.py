"""Spike A: Codex SDK structured turn under deny_all + read_only.

Live run (consumes a small amount of Codex quota; two short turns):

    PRFLOW_CODEX_BIN=$(readlink -f "$(command -v codex)") uv run python spikes/spike_a_structured_turn.py

What it proves (see docs/spikes/0001-foundation.md):
 1. creates a thread in this repository, then resumes it (non-ephemeral);
 2. submits a turn;
 3. Sandbox.read_only passed explicitly on thread AND turn;
 4. ApprovalMode.deny_all passed explicitly on thread AND turn;
 5. small JSON output_schema requested;
 6. structured result parsed;
 7. final_response is None handled (parse_structured), plus one read-only recovery turn policy;
 8. effective SDK/runtime version identified and the ExternalMessage gate evaluated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from openai_codex import ApprovalMode, Sandbox  # noqa: E402

from batch_launch import (  # noqa: E402
    BatchCodex,
    build_codex_config,
    check_runtime,
    parse_structured,
    repo_root,
    require_runtime_for_external_message,
    resolve_codex_bin,
    summarize_items,
    thread_kwargs,
    turn_kwargs,
    write_evidence,
    require_wire,
)

MODEL = "gpt-5.6-luna"  # listed, "fast and affordable"; never the account default
EFFORT = "low"

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "repo_dir_name": {"type": "string"},
        "spec_present": {"type": "boolean"},
        "top_level_entries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["repo_dir_name", "spec_present", "top_level_entries"],
}

DEVELOPER_INSTRUCTIONS = """You are running inside a read-only, network-disabled batch harness.
Use at most one shell command. Do not modify files. Do not request elevated permissions.
Answer only with JSON that matches the provided schema."""

PROMPT = (
    "Report the name of the current working directory, whether a file named SPEC.md exists "
    "at its top level, and the sorted list of top-level entry names (exclude hidden entries)."
)


def recovery_turn(codex: BatchCodex, thread, schema) -> dict:
    """At most one read-only recovery turn (spec 15.4). Only used if the first turn had no JSON."""
    result = thread.run(
        "Your previous turn produced no valid final JSON. Without running any commands or changing "
        "files, produce the JSON answer now from what you already learned.",
        **turn_kwargs(Sandbox.read_only, model=MODEL, effort=EFFORT, output_schema=schema),
    )
    return {"final_response": result.final_response, "items": summarize_items(result)}


def main() -> int:
    codex_bin = resolve_codex_bin()
    check = check_runtime(codex_bin)
    evidence: dict = {"runtime_check": check.as_dict(), "model": MODEL, "effort": EFFORT}
    try:
        require_runtime_for_external_message(check)
        evidence["external_message_gate"] = "pass"
    except RuntimeError as exc:
        evidence["external_message_gate"] = f"fail-closed: {exc}"
    print(f"runtime: sdk={check.sdk_version} bin={codex_bin} runtime={check.runtime_version}", file=sys.stderr)
    print(f"external_message_gate: {evidence['external_message_gate']}", file=sys.stderr)

    with BatchCodex(build_codex_config(repo_root(), codex_bin=codex_bin)) as codex:
        thread = codex.thread_start(
            **thread_kwargs(
                Sandbox.read_only,
                cwd=str(repo_root()),
                model=MODEL,
                developer_instructions=DEVELOPER_INSTRUCTIONS,
                ephemeral=False,  # so that thread_resume can be demonstrated
            )
        )
        evidence["thread_id"] = thread.id
        result = thread.run(
            PROMPT,
            **turn_kwargs(Sandbox.read_only, model=MODEL, effort=EFFORT, output_schema=OUTPUT_SCHEMA),
        )
        outcome = parse_structured(result, required_keys=("repo_dir_name", "spec_present", "top_level_entries"))
        evidence["turn1"] = {
            "status": result.status.value,
            "final_response_is_none": result.final_response is None,
            "structured_ok": outcome.ok,
            "structured_reason": outcome.reason,
            "structured": outcome.data,
            "items": summarize_items(result),
            "usage": result.usage.model_dump(mode="json") if result.usage else None,
        }
        if not outcome.ok:
            evidence["recovery_turn"] = recovery_turn(codex, thread, OUTPUT_SCHEMA)

        # Resume the same thread with explicit policy and run one tiny no-tool turn.
        resumed = codex.thread_resume(
            thread.id, approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only, model=MODEL
        )
        result2 = resumed.run(
            "Without running any command, return the same JSON object you returned before.",
            **turn_kwargs(Sandbox.read_only, model=MODEL, effort=EFFORT, output_schema=OUTPUT_SCHEMA),
        )
        outcome2 = parse_structured(result2, required_keys=("repo_dir_name", "spec_present", "top_level_entries"))
        evidence["turn2_resumed"] = {
            "thread_id": resumed.id,
            "status": result2.status.value,
            "structured_ok": outcome2.ok,
            "structured": outcome2.data,
            "items": summarize_items(result2),
        }
        evidence["wire"] = [
            {"method": w.method, "params": {k: v for k, v in w.params.items() if k != "input"}, "response": w.response}
            for w in codex.wire
        ]
        evidence["server_requests"] = [r.__dict__ for r in codex.server_requests]
        evidence["stderr_tail"] = codex.stderr_tail(10).splitlines()[-5:]

    # Assertions that make the spike pass/fail honestly (empty wire log is an error, not a pass).
    require_wire(codex.wire)
    checks = {
        "thread_start_deny_all": all(w.params.get("approvalPolicy") == "never" for w in codex.wire if w.method in ("thread/start", "thread/resume")),
        "every_turn_deny_all": all(w.params.get("approvalPolicy") == "never" for w in codex.wire if w.method == "turn/start"),
        "every_turn_read_only_no_network": all(
            w.params.get("sandboxPolicy", {}).get("type") == "readOnly" and w.params.get("sandboxPolicy", {}).get("networkAccess") is False
            for w in codex.wire if w.method == "turn/start"
        ),
        "thread_effective_reviewer_is_not_auto_review": all(
            (w.response or {}).get("approvalsReviewer") != "auto_review" for w in codex.wire if w.method == "thread/start"
        ),
        "no_server_requests": not codex.server_requests,
        "structured_result_parsed": outcome.ok or evidence.get("recovery_turn") is not None,
        "resume_worked": outcome2.ok,
    }
    evidence["checks"] = checks
    path = write_evidence("spike_a_structured_turn.json", evidence)
    print(json.dumps({"checks": checks, "structured": outcome.data, "evidence": str(path)}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
