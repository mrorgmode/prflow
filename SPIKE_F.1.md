Spike F PARTIAL is accepted in principle.

Do not start Phase 2 and do not modify product code or SPEC.md yet.

Perform one small amendment to Spike F only.

Construct and test the intended Phase 2 allowlist-style permission profile by **extending Codex's built-in `:workspace` profile**, rather than rebuilding workspace permissions from scratch.

Retain:

* `:root = "deny"`
* `:minimal = "read"`
* known credential-store denies
* repository `.env` / `.env.*` denies
* network disabled
* `ApprovalMode.deny_all`
* the Phase 0 inherited-MCP explicit-disable and fail-closed MCP/app preflight
* only the installation-specific Codex runtime/tool read paths actually required for normal execution

Do not combine the named permission profile with the legacy SDK `sandbox` / `sandboxPolicy` mechanism. Verify the effective profile rather than merely verifying that the configuration was accepted.

### Test both normal and linked-worktree Git layouts

This is important because `prflow` explicitly supports Git worktrees.

Test:

1. a normal checkout; and
2. a real linked worktree created with `git worktree`.

For the linked worktree, inspect the actual paths returned by Git, including:

* `git rev-parse --git-dir`
* `git rev-parse --git-common-dir`
* `git rev-parse --git-path prflow`

Do not assume the real Git metadata lives beneath the worktree root.

If `:root = "deny"` prevents Git from reading required linked-worktree metadata, determine the **smallest dynamically derived read-only exception** required for the actual Git metadata directories. Do not hardcode repository-specific paths.

A read-only exception for required Git metadata is acceptable. It must not make Git metadata writable.

Codex can read enough Git metadata for normal development commands, but cannot mutate Git metadata or prflow's authoritative state.

### Verify deterministically

In both checkout layouts, verify that:

* ordinary workspace files remain writable;
* repository files remain readable;
* `git status` works;
* `git log` works;
* `git diff` works;
* Git metadata needed for normal read operations is readable;
* Git metadata is not writable;
* attempts to modify `.git/config` fail;
* attempts to modify `.git/HEAD` fail;
* attempts to create or modify `.git/hooks/*` fail;
* attempts to modify the resolved per-worktree `prflow` state path (`git rev-parse --git-path prflow`) fail;
* known credential stores remain unreadable;
* repository `.env` canaries remain unreadable;
* direct network egress remains positively blocked.

Where `.git` is a pointer file rather than a directory, test the corresponding resolved Git metadata paths instead of assuming `.git/...` exists locally.

Use positive controls where necessary so that `BLOCKED` cannot be confused with a missing file, broken sandbox, or malformed probe.

Do not print credential contents.

The permission system is path-based, so retain the already documented hardlink residual rather than attempting to engineer around it.

Do not add another live model turn unless the deterministic `codex sandbox` and app-server probes disagree with the already-proven live profile enforcement.

### Runtime scope

The existing Spike F result was established on native Codex 0.154.0.

F.1 may establish the preferred profile design on that runtime, but do **not** claim that this alone proves the profile for the eventual Phase 2 SDK/runtime pair.

Record that the critical profile invariants must be revalidated deterministically against the exact coherent runtime selected by Spike B.1 before Phase 2 is enabled.

If the B.1 candidate runtime (currently expected to be 0.153.4) is cheaply available without promoting dependencies into product code, it is useful to repeat the deterministic profile/worktree probe there as well. Do not spend a model turn for this cross-version check.

### Outcome

If extending `:workspace` works, including the linked-worktree case:

* recommend this profile shape for future Phase 2 batch execution;
* mark Spike F as `PARTIAL-ACCEPTED`;
* explicitly retain the documented hardlink, platform, SDK-surface, and runtime-version residuals.

If linked worktrees require an additional read-only Git-metadata carve-out, document its exact derivation and include it in the recommendation.

If extending `:workspace` introduces a new problem that cannot be solved with a small, understandable read-only exception, document it and stop rather than engineering around it.

Then stop for design review.

Do not start Phase 2.
