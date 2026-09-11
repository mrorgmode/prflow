# Spike F: credential-read surface (2026-09-11)

Scope: SPEC.md Draft 0.3 §7 Spike F and §22.6 only. No Phase 2 code, no SPEC.md edits, no
product (`src/prflow`) changes, no new dependencies, no GitHub mutations by the probes. The findings apply only to the
tested host and must not be read as portability claims.

**Recommendation: PARTIAL.** On the tested runtime, a supported Codex *named permission profile*
stopped sandboxed tools from reading every known credential store that is present. This holds for
`codex sandbox`, for app-server `command/exec`, and for a live `gpt-5.6-luna`/low agent tool call on the
SDK thread/turn path. Normal workspace work still succeeds, and the unsandboxed runtime keeps its
authenticated control plane. It is not suitable for adoption as-is through the published SDK because:

1. the SDK's `Sandbox` presets **replace** the profile, so profile mode must pass no preset at all, which
   contradicts the current §15.1 wording;
2. SDK 0.147.0 cannot report which profile is active. Verification needs the raw `thread/start` response
   (a private client surface) or a newer SDK;
3. the protection is path-based, and residual read surfaces remain (pre-existing hardlinks; general home
   reads under the denylist variant);
4. the live evidence is one passing turn, preceded by one completed turn whose command output was not
   captured (see below).

Batch publication must stay guarded by human diff/outbox review in any case.

## Tested platform

| item | value |
| --- | --- |
| native runtime | `codex-cli 0.154.0` (`~/.local/bin/codex` → `~/.codex/packages/standalone/releases/0.154.0-x86_64-unknown-linux-musl/bin/codex`) |
| SDK | `openai-codex 0.147.0` (bundled runtime 0.147.0 unused; native binary via `launch_args_override`) |
| sandbox | bubblewrap 0.11.1 (Codex Linux sandbox, bwrap mount namespace) |
| kernel / OS | Linux 7.0.0-31-generic x86_64, Ubuntu 26.04.1 LTS (a Proxmox guest; the outer isolation is not relied on) |
| probe interpreter | `/usr/bin/python3` 3.14.4, UID 1000 in every run |

Mechanism sources: the installed `codex sandbox --help`, the generated app-server schema
(`codex app-server generate-json-schema [--experimental]`), and OpenAI's
[permissions documentation](https://learn.chatgpt.com/docs/permissions). The documentation was used for
syntax only. Every enforcement claim below comes from probes, never from a profile being accepted.

## Mechanism and exact profiles

Profiles are passed as process-level overrides (`-c`). Neither `~/.codex/config.toml` nor any credential
store was edited. `~` in profile paths resolves from `$HOME`. The recommended **allowlist** profile, as
used with the real Codex home:

```
permissions.prflow_batch={"description"="prflow Spike F allowlist","filesystem"={
  ":minimal"="read",
  "<codex release dir>"="read",            # bwrap re-executes the codex binary inside the sandbox
  "<SDK bundled bin dir (rg)>"="read",
  ":workspace_roots"={"."="write","**/.env"="deny","**/.env.*"="deny"},
  "~/.config/gh"="deny","~/.ssh"="deny","~/.gnupg"="deny","~/.codex/auth.json"="deny",
  "<CODEX_HOME>/auth.json"="deny"}}
default_permissions="prflow_batch"
```

The **denylist** variant is `extends=":workspace"` with the same denies and workspace globs. The exact
strings are in the evidence (`profiles_synthetic`, `launch_overrides`, `live_turn.profile`). The app-server
launch keeps the whole Phase 0 policy:

- `approval_policy="never"` and `ApprovalMode.deny_all` on thread and turn;
- apps, plugins, web, and multi-agent disabled;
- `mcp_servers={}`, the per-server disable, and the fail-closed MCP/app preflight on every thread;
- the `env -u` GitHub-token strip.

The only change is dropping the legacy `sandbox_mode` / `sandbox_workspace_write.network_access` keys in
favour of the profile plus `default_permissions` (`profile_batch_overrides`). Runtime 0.154.0 also
accepts the legacy keys next to `default_permissions`, and the profile wins. They are dropped so that
only one policy is in force.

Enforcement mechanism observed: bwrap mounts a read-only mode-000 tmpfs over each denied path. A denied
file appears as an empty mode-000 file, a denied directory as an empty mode-000 directory. `open` fails
with `EACCES`. In the allowlist profile, paths outside the allowed roots are simply absent (`ENOENT`).
Denying a path that does not exist (`~/.gnupg` on this host) does not break startup.

## Results

Every protected result is compared with a positive control on the **same files, UID and modes**: an
unsandboxed parent read and `-P :workspace` / `permissionProfile=":workspace"`, both `READABLE`. The probe
(`spikes/spike_f_probe.py`) opens a path, reads at most one byte, discards it, and prints only a status
word (`READABLE`, `READABLE_EMPTY`, `BLOCKED`, `MISSING`, `ERROR:*`). Its output is bracketed by
sentinels and must be the probe's own JSON shape. A sandbox that fails to start, or output that is
missing or malformed, is therefore classified `BROKEN`, never `BLOCKED`. A mechanism passes only if all
three hold:

- every present credential/alias target is protected;
- a non-empty set of workspace checks all return `OK`;
- the egress check is positively `BLOCKED`. A timeout or other error is `INCONCLUSIVE_NETWORK`.

### Synthetic canaries (temp tree under `~/.cache`, removed afterwards)

Targets: `~/.config/gh/hosts.yml`, `~/.ssh/` (listing) and a key file, `~/.gnupg/` and a key file,
`~/.codex/auth.json`, workspace `.env` and `config/.env.production`. Aliases: a symlink in the workspace,
a `..` path, `/proc/self/root/...`, and a `head` subprocess read.

| mechanism | denylist | allowlist |
| --- | --- | --- |
| `codex sandbox -P prflow_batch` | PASS | PASS |
| app-server `command/exec`, profile from `default_permissions` (stable API, no model) | PASS | PASS |
| app-server `command/exec`, `permissionProfile` (experimental field) | PASS | PASS |
| app-server `command/exec` with the SDK preset shape `sandboxPolicy` workspaceWrite / readOnly | **FAIL: all EXPOSED** | **FAIL: all EXPOSED** |
| Phase 0 overrides left in place *plus* profile + `default_permissions` | n/a | PASS (profile wins; launch accepted) |
| profile plus deny of the whole real `~/.codex` | **BROKEN** (`bwrap: execvp …/bin/codex: Permission denied`) | n/a |
| live agent tool call, SDK `thread.turn`, synthetic `HOME` + real `CODEX_HOME` | n/a | **PASS** (see below) |

Under PASS, every credential and alias target is `BLOCKED` and the directory listings are `BLOCKED`.
Subprocess reads were also `BLOCKED`. Workspace work stayed `OK` in the synthetic git repository:

- write and read back a file;
- `git status` and `git log`;
- run a Python child process;
- read a repository file.

Direct TCP egress stayed `BLOCKED` (the Spike C invariant holds under the profile). A general
non-credential home file stays readable under the denylist and is absent under the allowlist.

### Real paths (one-byte read, status only)

| target | parent | `:workspace` | profile (`codex sandbox` and app-server `command/exec`) |
| --- | --- | --- | --- |
| `~/.config/gh/hosts.yml` | READABLE | READABLE | PROTECTED |
| `~/.ssh/` listing and representative file | READABLE | READABLE | PROTECTED |
| `~/.gnupg/` | MISSING | MISSING | NOT_PRESENT (not on this host) |
| `~/.codex/auth.json` | READABLE | READABLE | PROTECTED |
| repository `.env` | MISSING | MISSING | NOT_PRESENT (no `.env` in this checkout; the synthetic `.env` is covered) |

### Codex control plane

The runtime process is not sandboxed and reads `$CODEX_HOME/auth.json` itself. With the profile denying
that file to tools, the app-server launched on the real `CODEX_HOME` returned:

- `account/read`: authenticated `chatgpt` account (identity not recorded);
- `model/list`: 5 models, including `gpt-5.6-luna`.

In the same session, `command/exec` could not read `auth.json`, and the live model turn ran normally. The
only Codex-home path sandboxed tools needed was the **runtime's install directory**
(`~/.codex/packages/standalone/releases/<version>/`), which bwrap re-executes. `:minimal` does not cover
it. Denying all of `~/.codex` therefore breaks every sandboxed command, and deny outranks read, so a
carve-out is impossible. For this installation, deny the authentication file while allowing the required runtime directories.

### SDK path: the preset replaces the profile

- `thread/start` with the SDK's own serialization of `Sandbox.workspace_write` / `read_only` reports
  `activePermissionProfile: null`, i.e. the legacy policy. The corresponding legacy `sandboxPolicy` on
  `command/exec` exposes every canary. `Thread.turn(sandbox=…)` sends the same legacy `sandboxPolicy`
  (documented as overriding "this turn and subsequent turns").
- `thread/start` **without** `sandbox` (the public SDK call with `sandbox=None`) reports
  `activePermissionProfile: {"id": "prflow_batch"}`. The experimental `permissions` field gives the same
  result.
- SDK 0.147.0 has no `permissions` field and its typed `ThreadStartResponse` silently drops
  `activePermissionProfile`. The SDK connection already negotiates `experimentalApi: true`. Reading the
  active profile needs the raw response (private `_request_raw`) or a newer SDK.
- The legacy `sandbox` field in responses reports `workspaceWrite` even for profile threads. It is not
  evidence of the effective filesystem policy.

### Live batch turn (2026-09-11)

One session: synthetic `HOME` (so `~` denies resolve to the canaries), real `CODEX_HOME`, allowlist
profile, Phase 0 launch policy. A model turn is spent only if both gates pass on the same session: a
deterministic `command/exec` gate, and a `:workspace` positive control on the same files. Both passed.
Public SDK calls throughout: `thread_start(deny_all, ephemeral, model=gpt-5.6-luna)`, then
`thread.turn(deny_all, effort=low)`, with no `sandbox` on either. The wire-policy capture records null for the omitted `sandbox` and
`sandboxPolicy` fields, `activePermissionProfile.id = prflow_batch`, and no server requests.

The attempts are all kept in the evidence (`previous_live_attempts`, then `live_turn`):

| started (UTC) | outcome |
| --- | --- |
| 2026-09-10 (×2) | refused by the account usage limit; no model tokens used |
| 2026-09-11 05:29 | turn `completed`, the agent ran the probe once (exit 0, reply `DONE`). **BROKEN (`no_begin_sentinel`)**: the harness saw no probe output. It used `thread.run()` and read `aggregatedOutput or ""`, so an absent aggregate and one without the sentinel are indistinguishable. |
| 2026-09-11 05:36 | turn `completed`, probe run once, exit 0. **PASS**: every credential and alias target `BLOCKED` (subprocess reads too), real `auth.json` `BLOCKED`, general home file absent, workspace checks all `OK`, egress `BLOCKED`, pre-existing hardlink `READABLE` (residual). About 17.8k tokens in total. |

Output-capture investigation. `thread.run()` collects only `item/completed` items and discards
`item/commandExecution/outputDelta` notifications. A local no-model check (unsandboxed
`thread/shellCommand` running `printf` only) showed the runtime emitting both the delta and a filled-in
`aggregatedOutput`. The harness now does three things:

- consumes the public `thread.turn().stream()` itself;
- keeps the deltas per item and parses them if the aggregate lacks the probe;
- records the aggregate's presence and length plus the delta count, never the output text.

In the passing turn the aggregate was present (1487 bytes) and no delta arrived. The earlier miss was
therefore **not reproduced and remains unexplained**. The passing turn is one observation, not a
reliability measurement.

## Residual read surfaces and limitations

- **Pre-existing hardlinks** to a credential file inside an allowed root stay readable (the protection is
  path-based). The agent cannot create new ones (`ln` fails), so the risk is limited to files that already
  exist.
- **Denylist leaves general home reads** open (`~/.aws`, `~/.netrc`, `~/.claude.json`, browser profiles,
  etc.). The allowlist closes these but depends on knowing the runtime install directory, and it makes
  `/tmp` unwritable (`:tmpdir` / `:slash_tmp` not granted; not needed by the tested workspace operations).
- The `.git` read-only protection that `:workspace` applies was not re-verified under the allowlist.
  prflow's parent process owns commits anyway.
- Globs (`**/.env`) were enforced here, unlike upstream issue openai/codex#22179 (0.130.0, macOS,
  `sandbox_mode` mixed with `[permissions]`). This result is Linux 0.154.0 only.
- The legacy-override-on-profile-thread check (`thread/settings/update` + `thread/resume`) could not run
  ("no rollout found" before a first turn). It was not iterated further.
- Unsandboxed paths are untouched by any profile: `thread/shellCommand` and `process/spawn` (by design),
  and the runtime process itself.
- One live agent turn passed. The model chose how to run the command, so other tool forms (for example a
  long-running unified-exec session) were not exercised.

## Recommended SPEC changes (prose only, not applied)

- §7 Spike F: record *PARTIAL* for the tested Linux/native runtime. The named-profile mechanism enforced
  credential-read denial in `codex sandbox`, app-server `command/exec` and one live SDK thread/turn tool
  call. Adoption is blocked by the SDK surface (preset replaces the profile; active profile not reported)
  and the residual surfaces, not by enforcement.
- §15.1: replace "an explicit sandbox" with "either an explicit sandbox preset **or** an explicitly
  selected named permission profile, never both". Profile mode must omit `sandbox` / `sandboxPolicy` on
  thread and turn, and must verify `activePermissionProfile.id` on `thread/start`/`thread/resume`,
  failing closed if it is absent or different. Until the SDK exposes that field, this depends on a raw
  response or an SDK upgrade, which should be a named Phase 2 prerequisite.
- §22.6 / Phase 2 prerequisite: protect `$CODEX_HOME/auth.json` while allowing the runtime directories actually required by the installation. Prefer an
  allowlist-style profile. Keep human diff/outbox review because hardlink and general-read residuals
  remain. `doctor` should report whether a profile is active and whether the known stores are readable.
- Batch tool-output evidence should come from the event stream (output deltas plus the completed item),
  not from `TurnResult.items` alone, and missing output must count as failure, never as success.

## Files and reproduction

- `spikes/spike_f_probe.py`: stdlib in-sandbox probe (statuses only).
- `spikes/spike_f_credential_read.py`: canary tree, profiles, all runners, live turn, evidence (carries
  earlier live attempts forward into `previous_live_attempts`).
- `tests/test_spike_f.py`: offline tests. They cover:
  - sentinels and broken runs;
  - malformed or non-object probe structures;
  - missing/blocked/readable/empty classification;
  - verdicts requiring non-empty workspace checks and a positively `BLOCKED` network;
  - the live-output source choice and the carry-forward of earlier attempts;
  - real mode-000 files and directories, with no content in the output;
  - TOML/profile construction, Phase 0 override retention, and the canary layout.
- `docs/spikes/evidence/spike_f_credential_read.json`: bounded, path-shortened evidence (no contents,
  no account identity).

```bash
uv run python -m pytest -q                              # 142 passed
uv run python spikes/spike_f_credential_read.py         # offline; no model turn
uv run python spikes/spike_f_credential_read.py --live-turn   # + one gpt-5.6-luna/low turn
```
