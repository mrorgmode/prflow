# Working preferences

- Phase 1 is accepted. Spike F.1 is accepted as PARTIAL-ACCEPTED. Current authorized scope: SPIKE_B.1.md only (stable SDK/runtime authority and isolation verification). Do not implement Phase 2, modify SPEC.md without explicit approval, or build container/VM/custom sandbox management. Keep experimental compatibility code out of product architecture.
- Codex acts as project manager and delegates substantial work to the logged-in Claude CLI to conserve the user's limited Codex quota.
- User's model preference: Fable for hard tasks, Opus for less hard tasks, then Sonnet. Avoid Haiku except for trivial tasks.
- For Claude batch runs, explicitly choose effort (usually `--effort medium`) and ALWAYS pass `--disallowedTools "Agent"` to prevent expensive nested agents. Pass bounded prompts via stdin. Restrict permissions/tools to the task.
- Claude is slow: do not kill or interrupt it merely because it takes a long time. Wait for it; do not interpret silence as a hang.
- Keep Codex live spike turns few and short, use an explicitly selected inexpensive supported model, and never silently inherit the expensive account default.
- The user explicitly authorized pushing changes and creating feature branches, issues, and PRs in mrorgmode/prflow. The exact test review/reply/resolution in docs/spikes/github-fixture-plan.md was approved and completed. Do not ask again for authorized repository work; unrelated external messages still need authorization.
- Pause for quota/effort changes when requested. Current Phase 0 blockers and completed GitHub actions are recorded in docs/spikes/HANDOFF.md.
