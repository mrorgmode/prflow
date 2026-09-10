# Spike 0001: foundation (SPEC.md section 7, Phase 0)

Date: 2026-09-10. Implementation: Claude Fable 5.1; supervision and live GitHub verification: Codex.
The user approved the disposable GitHub fixture and repository publication. All live turns used `gpt-5.6-luna` at effort `low` on the native
`codex-cli 0.154.0` runtime through the published `openai-codex 0.147.0` SDK.

## Summary

| Spike | Status | Evidence |
|---|---|---|
| A structured turn | **PASS** | `evidence/spike_a_structured_turn.json` |
| B untrusted input | **BLOCKED** (fallback run is exploratory only) | `evidence/spike_b_untrusted_input.json` |
| C batch isolation | **INCOMPLETE** (gh/shell/MCP/app paths verified; final workspace_write tool surface run **BLOCKED by usage limit**) | `evidence/spike_c_isolation.json`, `evidence/spike_c_toolsurface.json`, `evidence/spike_c_mcp_inherit.json`, `evidence/spike_c_isolation_blocked.json`, `evidence/probe_runtime_batch-0.154.0.json` |
| C inherited MCP control | **PASS** with a required fix (see below) | `evidence/spike_c_mcp_inherit.json` |
| C skill reuse | **REJECTED** for batch | same |
| D GraphQL | **PASS** (read, reply, refresh, resolve, refresh) | `evidence/spike_d_live.json`; [test PR #1](https://github.com/mrorgmode/prflow/pull/1) |
| E interactive handoff | **PARTIAL** (real TUI startup proven in a pty; prompt delivery proven by argv only) | `evidence/spike_e_interactive_smoke.json` |

**Overall: batch mode is unavailable under the strict spec** because the published SDK does not
provide `ExternalMessage` (6.1, 15.2). The launch policy itself (deny_all on thread and turn,
explicit sandbox, network off, no apps/MCP) is implemented and verified on the wire.

## Environment facts that differ from the spec's assumptions

1. **`ExternalMessage` does not exist in the published SDK.** PyPI latest is `openai-codex 0.147.0`
   (2026-08-18). Its `UserInput` variants are text, image, localImage, audio, localAudio, skill,
   mention. The native `codex-cli 0.154.0` app-server v2 schema (generated with
   `codex app-server generate-json-schema`) has the same list and no external-message variant.
   These installed interfaces do not provide the capability assumed by the spec; the source-tree references do not establish installed support.
2. **The SDK bundles runtime 0.147.0**, below the spec's 0.151.0 floor. The native 0.154.0 binary
   is used through `CodexConfig.launch_args_override` (`PRFLOW_CODEX_BIN`). Two runtimes sharing
   `~/.codex` produced one incompatibility warning (0.147.0 could not parse the 0.154.0 models cache).
3. **The SDK's low-level default approval handler accepts approvals** (`CodexClient._default_approval_handler`
   returns `accept`), and the high-level `Codex` class gives no way to replace it. `spikes/batch_launch.py`
   subclasses `Codex`, installs a decline-and-record handler, and aborts the turn on any unknown
   server request. A test pins the private attribute names this relies on.
4. **The GitHub plugin is a remote connector app, not a local skill.** The plugin cache holds only
   `plugin.json` and `.app.json`; there is no `gh-address-comments` payload on disk and `skills/list`
   does not show it. Its tools are served by the `codex_apps` MCP server (`api.githubcopilot.com/mcp`)
   inside the Codex process, i.e. **outside the command sandbox**. Sandbox network isolation alone would
   not block them.
5. **bubblewrap was missing**; installed as an OS package. `codex doctor` had reported a legacy helper
   fallback. This is a native sandbox dependency, not a spec blocker.
6. Model catalog on this account: `gpt-6-astra` (default, not used), `gpt-5.6-sol/terra/luna`, `gpt-5.5`.
   `gpt-5.6-luna` is a code-mode model (`tool_mode: code_mode_only`, `multi_agent_version: v1`), which
   affects how tools are exposed (see C).

## Batch launch configuration (spikes/batch_launch.py)

- Thread: `approval_mode=ApprovalMode.deny_all`, explicit `Sandbox`, `ephemeral` by default.
  Turn: `approval_mode=ApprovalMode.deny_all`, explicit `Sandbox`. Both helpers refuse overrides of
  those keys and refuse `full_access`.
- Runtime argv: `env -u GH_TOKEN -u GITHUB_TOKEN -u GITHUB_PAT_TOKEN -u GH_ENTERPRISE_TOKEN -u GITHUB_ENTERPRISE_TOKEN <codex> -c ... app-server --listen stdio://`.
- `-c` overrides: `approval_policy="never"`, `sandbox_mode="read-only"`,
  `sandbox_workspace_write.network_access=false`, `features.apps=false`, `features.plugins=false`,
  `features.remote_plugin=false`, `features.skill_mcp_dependency_install=false`,
  `include_apps_instructions=false`, `mcp_servers={}`, `web_search="disabled"`, `tools.web_search=false`,
  `features.browser_use=false`, `features.computer_use=false`, `features.image_generation=false`,
  `features.multi_agent=false`, `agents.max_depth=0`, `shell_environment_policy.exclude=[tokens...]`.
  Extra overrides may not redefine any of these keys.
- **`mcp_servers={}` does not remove inherited servers** (proven, see C). prflow must therefore read the
  effective `mcp_servers` names on a first launch, relaunch with `mcp_servers.<name>.enabled=false` for each
  (`disable_overrides_for`), and always run `BatchCodex.preflight_no_tool_servers(thread_id)`, which raises if
  any MCP server lists tools or reports `connected`, or if any app is listed. Disabled servers remain visible in
  `mcpServerStatus/list` with `runtimeStatus: disabled` and zero tools; that is tolerated.
- Every evidence file written after the review records this configuration under `launch`; earlier files
  carry a `provenance` block stating which subset was active when they were produced.

## Spike A: PASS

Two turns. Wire log shows `thread/start` and `thread/resume` with `approvalPolicy: never`, every
`turn/start` with `approvalPolicy: never` and `sandboxPolicy: {type: readOnly, networkAccess: false}`.
The thread/start response reports `approvalsReviewer: user` (not `auto_review`). Structured JSON parsed;
resume worked; zero server requests. `final_response is None` handling and the one-recovery-turn policy
are implemented (`parse_structured`, `recovery_turn`) and unit-tested; they were not triggered live.
The ExternalMessage gate correctly fails closed: "openai-codex 0.147.0 does not export ExternalMessage".

## Spike B: BLOCKED; fallback run is exploratory only

Without `ExternalMessage` the review thread was delivered as a delimited data block in a **user-role**
message with policy in `developer_instructions`. That does not reproduce the tool-level versus
user-level authority separation the spec asks to prove, so the run cannot count as a pass. As a
semantic regression sample (one turn, low effort) the injected comment did not change the disposition
(`fix_now`), did not flip `resolve_after_reply`, did not move the alias, and did not appear in the reply.
The checker (`check_semantic_integrity`) is unit-tested against every injection dimension and is ready to
be re-run unchanged once `ExternalMessage` ships. The fixture only exercises the "stay false" direction.

## Spike C: INCOMPLETE; skill reuse REJECTED

Verified, with tool-level evidence:

- Parent process `gh api user` succeeds (login recorded, never the token).
- Direct `codex sandbox -P :read-only|:workspace`: DNS fails, TCP connect is `EPERM`, `gh` reports
  "error connecting to api.github.com", repo writable only under `:workspace`, `~/.codex` read-only.
- Agent turn (deny_all + `workspaceWrite`, `networkAccess: false` on the wire): the agent's own
  `gh api user` and `curl` failed identically; it attempted escalation and the runtime logged
  `approval policy is Never; reject command` twice; **no server request reached the client**.
- MCP/app inventory for the batch thread: 0 MCP servers, `app/list` empty. Baseline SDK-default launch on
  the same runtime: `codex_apps` MCP server connected with 140 tools including `github.create_issue`,
  `github.add_review_to_pr`, `github.create_commit` (write-capable), which is the surface the batch
  config removes.
- Ground-truth tool surface from the runtime's raw rollout trace (`CODEX_ROLLOUT_TRACE_ROOT`, one
  read_only turn with the final override set): nested tools are exactly `apply_patch, create_goal,
  exec_command, get_goal, update_goal, view_image, write_stdin`. No GitHub, MCP, web, or multi-agent tools.

Inherited MCP control (offline, no model turns; `spikes/spike_c_mcp_inherit.py`): a harmless local stdio
server (`spikes/dummy_mcp_server.py`) was injected via (1) a temporary `CODEX_HOME` config with no auth files
and (2) a temporary repo-level `.codex/config.toml` in this trusted checkout. Positive control: the dummy is
exposed to a batch thread with its tool (`runtimeStatus: connected`). With the full batch overrides including
`mcp_servers={}` the dummy is **still exposed** (merge, not replace); the new preflight raised (fail closed).
With `mcp_servers.prflow_dummy.enabled=false` added it shows `runtimeStatus: disabled`, zero tools, and the
preflight passes. Both controls agree. Temporary files were removed.

Not verified, therefore INCOMPLETE:

- The one planned live `workspace_write` turn under the final config (runtime ALL_TOOLS dump through the
  code-mode `exec` tool plus gh/curl/escalation, with automatic trace deletion) was **refused by the Codex
  service: usage limit reached, retry after 23:48**. It was not retried. The earlier isolation run predates
  the `web_search="disabled"` and `agents.max_depth=0` overrides, and the only traced run (read_only, final
  overrides) showed the nested tools `apply_patch, create_goal, exec_command, get_goal, update_goal,
  view_image, write_stdin` with no GitHub/MCP/web/multi-agent tools. The workspace_write surface under the
  final config is therefore still an open item, not a claim.
- `features.tool_suggest` and `auth_elicitation` remain true; effect unknown.
- The sandbox can read `~/.config/gh/hosts.yml` (stored gh credential). Network isolation is the boundary,
  as the spec states, but this is a real residual and should be recorded in `doctor` later.
- Output matching truncates command output at 1500 characters.

Skill reuse: there is no local `gh-address-comments` payload to load with `SkillInput`, and the plugin's
tools are the connector app. **Decision: do not load the GitHub plugin or its skills in batch mode;
prflow supplies review data and uses its own prompts.** The full plugin stays available to interactive Codex.

## Spike D: PASS

The user approved the exact fixture plan in `github-fixture-plan.md`. The trusted parent seeded the
empty repository with unchanged SPEC.md and opened [draft PR #1](https://github.com/mrorgmode/prflow/pull/1)
with a four-line disposable fixture. It created the approved inline review, fetched its unresolved
thread through `gh api graphql`, and used `spike_d_graphql.py` to reply and resolve it.

`evidence/spike_d_live.json` records the authoritative thread/comment IDs, path/line, authors, bodies,
timestamps, mutation responses, and three snapshots. The reply changed the fingerprint; the parent
refetched before resolving; the last read confirmed resolution and another fingerprint change.
Only the approved fixture thread was mutated. The draft PR remains unmerged. The earlier public-PR
read was supplementary. This small fixture does not establish pagination of threads with >50 comments.

## Spike E: PARTIAL

`spikes/spike_e_interactive.py` builds the interactive prompt (work-item context, policy, delimited review
data, staging preference, limitation notice) and the argv `codex --cd <repo> "<prompt>"`, and can launch
it inheriting the terminal. `--smoke` started the real native TUI in a pseudo-terminal in this repository
with the user's normal interactive configuration: it rendered its banner in about 1.2 s and exited with
status 0 on Ctrl-C. No prompt was passed and no model turn ran, because the TUI has no option to load a
positional prompt without submitting it. Prompt delivery is therefore proven only by argv construction; a
full handoff (prompt submitted to the interactive model) is deliberately left to the human.

## Decisions needed from a human

1. Accept that batch mode is blocked until `openai-codex` ships `ExternalMessage`, or explicitly relax
   6.1/15.2 to allow the delimited-data fallback (with its weaker authority guarantee). No SPEC change made.
2. Runtime policy: pin the native `codex` binary (0.154.0) via `codex_bin`, or wait for a published SDK
   whose bundled runtime is >= 0.151.0. Using two runtimes on one `~/.codex` has shown cache incompatibility.
3. GitHub fixture approval and Spike D are complete; no further approval is needed for those completed actions.
4. Re-run `spikes/spike_c_isolation.py` once after the usage limit resets (one short `gpt-5.6-luna` turn) to
   verify the workspace_write tool surface under the final config before calling C complete.
5. Accept the `mcp_servers.<name>.enabled=false` + preflight approach as the batch MCP policy for Phase 2.

## Offline tests

`uv run pytest -q`: 36 passed. They cover deny_all mapping, wire serialization of `networkAccess: false`
for both sandboxes, locked policy kwargs, fail-closed unknown server requests, token stripping in argv,
version gating, structured-output handling, Spike B semantic checks, Spike D fingerprints/argv, and the
Spike E prompt/argv, plus trace analysis (non-vacuous), trace auto-cleanup, per-server MCP disable, the
fail-closed preflight, and the dummy MCP server protocol. Raw runtime traces (they contain model reasoning)
are now created and deleted by `rollout_trace.TraceCapture` in `finally`; evidence JSON is bounded and redacted.
