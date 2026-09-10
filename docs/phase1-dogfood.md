# Phase 1 dogfood record — 2026-09-10

Real, read-only runs of the Phase 1 CLI against `mrorgmode/prflow`, from the
`phase1/orientation` checkout (draft PR #3, stacked on PR #2). No GitHub mutation was
made; the only writes are this checkout's `.git/prflow/state.json`. Runs used
`gh 2.100.0` (authenticated as `mrorgmode`), `git 2.53.0`, Python 3.14.4.

Acceptance (SPEC §27 Phase 1): **met.** `prflow` resolved PR #3 from the current
branch and showed its review thread, including the comment text, diff hunk and
comment link, with no browser copy/paste.

## `prflow doctor` (exit 0)

```text
Phase 1 read-only orientation: READY
interactive Codex: available
batch triage/fix (Phase 2): UNAVAILABLE
[phase1]      ok: git 2.53.0; checkout (phase1/orientation); state dir; gh 2.100.0;
              gh_auth github.com: mrorgmode; pull_request mrorgmode/prflow#3 via current branch
[interactive] ok native_codex codex-cli 0.154.0; info ~/.codex/auth.json present (not read)
[batch]       info sdk openai-codex 0.147.0, bundled runtime 0.147.0; native CLI is 0.154.0 (independent)
              unavailable external_message (0.147.0 does not export ExternalMessage)
              unavailable runtime_version (bundled 0.147.0 < 0.151.0)
              unverified  pair_probe (not implemented in Phase 1; batch never green)
              warn        credential_read (Phase 0 observation, Spike F pending, not re-probed)
[github_plugin] info github 0.1.12-5f7cd798dc99 cached; connector app manifest present;
              no local skill payload (gh-address-comments not available as a file)
[repository]  info checks: default (1 command, not run); no .pre-commit-config.yaml;
              commit.gpgsign/gpg.format/user.signingkey unset
[sandbox]     ok bubblewrap 0.11.1
```

This matches the dogfood review on PR #3 (thread T1): read-only readiness is separate
from batch readiness, and the missing `ExternalMessage` does not block status or
review inspection.

## `prflow status` (exit 0)

```text
repo     mrorgmode/prflow
branch   phase1/orientation  HEAD d566c24cc635  (uncommitted changes)
PR #3    Phase 1: read-only PR orientation and review inspection  [OPEN, draft]  (via current branch)
         phase1/orientation -> phase0/foundation  head d566c24cc635 (= local HEAD)
stack    parent #2; children none
reviews  not fetched yet; run `prflow refresh`
state    /home/dev/proj/prflow/.git/prflow/state.json (revision 0)
```

The stack parent comes from observation only: PR #2's head is `phase0/foundation`.

## `prflow refresh`, `review list`, `review show T1`

```text
$ prflow refresh
Refreshed PR #3 mrorgmode/prflow  phase1/orientation -> phase0/foundation: 1 unresolved of 1 review threads (state revision 1)
  new: T1

$ prflow review list
T1   SPEC.md:227  [discovered]  1 comment
     mrorgmode: Codex: Phase 1 dogfood review: please keep read-only readiness separate from batch readiness in doctor. …

$ prflow review show T1
T1  SPEC.md:227  PR #3 mrorgmode/prflow  phase1/orientation -> phase0/foundation
thread   PRRT_kwDOUVk9os6hPHSq  unresolved
state    discovered
source   sha256:b7a1bdbc08ffbe5c77e39c5039d111a77f904a65d2f3026d8f2adfbfbd896715
diff     (last 8 lines of the hunk)
[1] mrorgmode  2026-09-10T20:33:13Z
    Codex: Phase 1 dogfood review: …
    https://github.com/mrorgmode/prflow/pull/3#discussion_r3983240822
```

A second `prflow refresh --json` produced `state_revision` 2 with empty
`new/changed/resolved/reopened/missing`. T1 kept its alias and fingerprint.
`prflow review list` from `spikes/` read the same state.

## Resolved thread (PR #1) via explicit selection

```text
$ prflow refresh --pr 1
Refreshed PR #1 … 0 unresolved of 1 review threads (state revision 3)
$ prflow review list --pr 1          -> No unresolved review threads.
$ prflow review list --pr 1 --all    -> -    spikes/fixtures/review_target.py:4  [resolved]  2 comments
$ prflow review show --pr 1 PRRT_kwDOUVk9os6hONVS
thread   PRRT_kwDOUVk9os6hONVS  resolved by mrorgmode   (both comments shown)
```

A thread first seen already resolved gets no `Tn` alias; it would get the next one if
it were ever reopened. An explicit `--pr` refresh does not rebind the current branch.
PR #3's T1 was unaffected.

## Error paths observed live

- `prflow review show T9 --json` → exit 1, stdout
  `{"ok": false, "command": "review show", "error": {"code": "unknown_thread", …}}`.
- A temporary detached linked worktree (`git worktree add --detach`, removed
  afterwards): `review list --json` → `detached_head`. `refresh --pr 3` there wrote
  separate state at `.git/worktrees/wt/prflow`, starting again at T1 / revision 1.
- `python -m prflow status --json` gave the same result as the `prflow` script.

## Friction noted (for Phase 5, not acted on)

- `review list` and `review show` read only the cache. That is explicit and fast, but
  after a reviewer comments you need `prflow refresh` or `--refresh`. `status` shows
  the cache age.
- Resolved threads without aliases must be addressed by node ID in `review show`.

## Final verification

`uv run --frozen pytest -q`: **102 passed** (including the retained Phase 0 tests).
Final review added refusal of malformed GraphQL responses and corrupt alias counters/duplicate aliases.
An independent run using system Python with `PYTHONPATH=src` and no installed Codex SDK
passed `doctor --json`, `status --json`, `refresh --json`, `review list --json`, and
`review show T1 --json`; Phase 1 remained ready while batch was unavailable.

Phase 1 acceptance is met. Stop before Phase 2 and its prerequisite spikes.
