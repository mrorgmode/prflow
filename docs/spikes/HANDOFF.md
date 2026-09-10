# Phase 0 handoff — 2026-09-10 (final)

Stop before Phase 1. Read 0001-foundation.md for results and design decisions. SPEC.md is unchanged.

41 offline tests pass. SDK pinned to 0.147.0; native runtime 0.154.0. Claude Fable implemented the
experiments, Opus reviewed them, Codex supervised and executed the GitHub test. Follow AGENTS.md for
future delegation.

Status: A PASS. B BLOCKED (installed SDK/runtime provide no ExternalMessage; fallback semantics are
exploratory only, not a substitute). C PASS on the final config after the quota reset: workspace_write
turn, runtime-trace tool list `apply_patch, exec_command, view_image, write_stdin`, gh/curl denied,
escalation rejected by the runtime, no MCP/apps, no edits; the preflight is now centralized in
`BatchCodex.thread_start/thread_resume`. Inherited MCP definitions survive `mcp_servers={}`; per-server
disable plus the fail-closed preflight is the policy. D PASS on https://github.com/mrorgmode/prflow/pull/1
(do not repeat those mutations). E PASS: real TUI with the generated prompt, one tiny `gpt-5.6-luna` turn,
assistant answered the derived marker, zero tool calls, workspace unchanged.

Evidence history is preserved: `spike_c_isolation_run1_early_overrides.json` (first run, earlier override
set), `spike_c_isolation_blocked.json` (usage-limit refusal), `spike_c_isolation.json` (final).

Branch phase0/foundation, draft PR #2; the root session handles commit/push/PR updates. Next: discuss B's
missing API and the MCP merge behaviour before deciding whether to revise the spec or proceed to Phase 1.
