# Phase 0 handoff — 2026-09-10

Stop before Phase 1. Read 0001-foundation.md for results and design decisions. SPEC.md is unchanged.

36 offline tests pass. SDK pinned to 0.147.0; native runtime 0.154.0. Claude Fable implemented the experiments, Opus reviewed them, and Codex supervised and executed the GitHub test. Claude session 2647ced1-05fc-4ab6-9993-e55d26a06933 is finished. Follow AGENTS.md for future delegation.

A passed. B is blocked: installed SDK/runtime do not provide ExternalMessage; fallback semantics are exploratory only. C shell/gh network denial, escalation denial, and MCP/app exclusion have evidence. Inherited MCP definitions survive mcp_servers={}; explicit per-server disable plus fail-closed preflight passed positive controls. The final workspace-write tool-surface verification was refused by the Codex usage limit (reported retry time 23:48); it was not retried. Raw traces now auto-delete. E actual TUI startup and exit passed, but live generated-prompt handoff remains unverified.

D passed on https://github.com/mrorgmode/prflow/pull/1. User explicitly approved the fixture and broader pushes/branches/issues/PRs in this repository. Main was seeded with unchanged SPEC.md; the draft fixture PR contains a tiny test file, approved inline review and reply, and a resolved test thread. Leave it unmerged. Evidence: evidence/spike_d_live.json. Do not repeat completed mutations or ask again for their approval.

Current branch: phase0/foundation, containing the experiments and report for review. No Phase 1 implementation. Remaining after quota reset: one final-config workspace-write C turn; actual generated-prompt E handoff. Discuss B's missing API and the observed MCP configuration behavior before deciding whether to revise the spec or proceed.
