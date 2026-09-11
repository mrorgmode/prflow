# prflow

Local-first workflow harness for developing GitHub pull requests with Codex.
See `SPEC.md` (Draft 0.3). This checkout implements **Phase 1: read-only
orientation and review inspection**. `prflow` finds the current PR, fetches every
review thread through `gh`, assigns stable `T1, T2, …` aliases and keeps
per-worktree state. It makes **no GitHub changes** and runs **no Codex turns**.
Triage/fix, checks execution, the outbox and interactive handoff come in later phases.

## Install

Runtime requirements: Python >= 3.11 (standard library only), `git`, and an
authenticated `gh` (`gh auth login`). The Codex SDK is **not** needed for Phase 1.

```bash
uv sync                      # dev: installs prflow (editable) + pytest + the pinned SDK used by Phase 0 tests
uv run prflow --help         # or: uv run python -m prflow --help

pip install .                # plain install: pulls in no runtime dependencies
pip install '.[codex]'       # optional: openai-codex 0.147.0, only inspected by `prflow doctor`
```

Linux/WSL/macOS only: state locking uses `fcntl.flock`, so native Windows is unsupported.

## Phase 1 workflow

Run inside a checkout or linked worktree of the PR's repository, on the PR branch:

```bash
prflow doctor            # Phase 1 readiness vs. (unavailable) batch readiness; changes nothing
prflow status            # checkout, PR (from the current branch), stack context, cached review summary
prflow refresh           # fetch all review threads (full pagination) into local state
prflow review list       # unresolved threads from the local cache; --all adds resolved/vanished ones
prflow review show T1    # one thread with all comments, diff hunk and links (alias or PRRT_… node ID)
```

- **Which PR:** without `--pr`, prflow picks the one open same-repository PR whose
  head is the current branch (or its configured upstream branch name). It refuses to
  guess on a detached HEAD, when several PRs match, or when only fork PRs match.
  `--repo OWNER/REPO` and `--pr N` select explicitly. An explicit `--pr` refresh does
  not rebind the branch.
- **Network vs. cache:** `doctor`, `status` and `refresh` read GitHub through `gh`.
  `review list` and `review show` read only the local cache. Pass `--refresh` to
  fetch first, and use `status` to see how old the cache is.
- **Aliases:** a thread gets the next `Tn` of its PR the first time it is seen
  unresolved. Aliases are never renumbered or reused, even if threads vanish. Each PR
  has its own alias series, and aliases are local conveniences for this checkout only.
- **Refresh semantics (SPEC §11.6):** threads resolved on GitHub become `done`
  (`resolved_on_github`) if untouched, or `stale` if local work exists. Reopened `done`
  threads return to `discovered` with the same alias. Nothing local is discarded.
- **State:** `$(git rev-parse --git-path prflow)/state.json`, per worktree, with
  `schema_version` and a monotonically increasing `state_revision`. Writes hold a
  `flock` for the whole read-modify-write and replace the file atomically. Network
  reads happen outside the lock and are merged into the latest state, so concurrent
  updates are never lost. `refresh --expected-revision N` refuses stale writers.
  Corrupt or newer-schema state is reported and never overwritten.
- **JSON:** every command takes `--json`. stdout then carries exactly one document,
  including on failure (`{"ok": false, "command": …, "error": {"code", "message",
  "hint"?, "details"?}}`), and diagnostics go to stderr. Exit codes: 0 ok, 1 error
  (for `doctor`: Phase 1 not ready), 2 usage. Stable error codes include
  `not_a_git_repo`, `gh_missing`, `gh_auth`, `repo_unresolved`, `repo_not_found`,
  `detached_head`, `no_pr`, `pr_ambiguous`, `pr_not_found`, `not_refreshed`,
  `unknown_thread`, `state_corrupt`, `state_future_schema`, `state_revision_mismatch`.
- Human output escapes terminal control characters from GitHub text. `--json`
  returns it verbatim.

`prflow doctor` never runs model turns, starts no Codex runtime, reads no credential
files and changes no Git/Codex configuration. Batch mode is reported `UNAVAILABLE` or
`UNVERIFIED`, never ready. The published `openai-codex 0.147.0` lacks
`ExternalMessage`, its bundled runtime is older than 0.151.0, the pair feature probe
is not implemented, and the Spike F credential-read findings await design review.
The Phase 1 doctor retains its original conservative batch-readiness checks.

Real results on this repository's PRs are recorded in [docs/phase1-dogfood.md](docs/phase1-dogfood.md).
Future user-requested ideas, including opening review locations in Emacs, are tracked in
[docs/future-plans.md](docs/future-plans.md).

## Code layout

`src/prflow/`: `cli.py` (argparse, rendering, exit codes), `review.py` (PR
resolution policy, snapshot merge, aliases), `github.py` (read-only `gh` adapter,
GraphQL pagination), `git.py`, `state.py`, `doctor.py`, `errors.py`. Phase 1 does
not import the Phase 0 spikes.

## Tests

```bash
uv run pytest -q
```

All tests are offline. The Phase 1 tests use real temporary Git repositories and
worktrees, with a fake `gh` (`tests/fake_gh.py`) and a fake `codex` placed first on
`PATH`. They cover thread and nested-comment pagination, null authors and lines,
structured errors, alias stability across new threads and PR switches, external
resolution and reopening, fingerprint changes, concurrent writers, expected-revision
refusal, corrupted state, entry points without the SDK, and honest `doctor` readiness.
The Phase 0 spike tests remain in `tests/test_batch_launch.py`,
`tests/test_phase0_controls.py` and `tests/test_spike_semantics.py`.

## Phase 0 references

Phase 0 (feasibility spikes) is documented in `docs/spikes/0001-foundation.md` and
`docs/spikes/HANDOFF.md`. Results: A, C, D, E pass. B is **BLOCKED** on the missing
`ExternalMessage`, and batch reuse of the GitHub plugin is rejected. Spike code lives
in `spikes/` and is provisional evidence, not product code.

Phase 0 environment (2026-09-10): Python 3.14.4, uv 0.12.12, openai-codex 0.147.0
with bundled runtime 0.147.0, native `codex` CLI 0.154.0, gh 2.100.0, git 2.53.0,
bubblewrap 0.11.1, pytest 9.1.1.

Spike commands. These need no model turns, though the runtime and GitHub probes still use the network:

```bash
PRFLOW_CODEX_BIN=$(readlink -f "$(command -v codex)") uv run python spikes/probe_runtime.py --baseline
codex sandbox -P :workspace -C "$PWD" -- bash spikes/sandbox_probe.sh
uv run python spikes/spike_d_graphql.py --repo openai/codex --pr 35882   # mutations stay dry-run without PRFLOW_ALLOW_GITHUB_WRITE=1
PRFLOW_CODEX_BIN=$(readlink -f "$(command -v codex)") uv run python spikes/spike_c_mcp_inherit.py
uv run python spikes/spike_e_interactive.py [--smoke]
```

Live spikes (each consumes a few short `gpt-5.6-luna`, effort `low` turns; never the account default model):

```bash
export PRFLOW_CODEX_BIN=$(readlink -f "$(command -v codex)")
uv run python spikes/spike_a_structured_turn.py
uv run python spikes/spike_b_untrusted_input.py      # exit 2 = fallback delivery, exploratory only
uv run python spikes/spike_c_isolation.py
uv run python spikes/spike_e_interactive.py --handoff
```

Evidence lands in `docs/spikes/evidence/*.json` (bounded and redacted).

Spike F (credential-read surface, required before Phase 2) is documented in
[docs/spikes/0002-credential-read.md](docs/spikes/0002-credential-read.md). Reproduce it with
`uv run python spikes/spike_f_credential_read.py` (no model turn; add `--live-turn` for one
short `gpt-5.6-luna`/low batch turn). Its offline tests are in `tests/test_spike_f.py`.
Amendment F.1 (a profile extending `:workspace`, normal checkout plus linked worktree, no model turn)
is reproduced with `uv run python spikes/spike_f1_workspace_profile.py [--compare-bin PATH]`. Its
tests are in `tests/test_spike_f1.py`.
