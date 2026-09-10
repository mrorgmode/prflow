# Future ideas

These are user-requested ideas, not additions to the current Phase 1 scope.

## Open the reviewed location in Emacs

Requested 2026-09-10: when a review item is selected or processed, optionally open
its file in Emacs and navigate to the relevant line. Allow displaying it in a
separate window or frame while retaining the review context.

A future Emacs/Transient frontend (SPEC.md §30.1) or small `emacsclient` handoff
could consume the machine-readable review record: worktree root, path, current
line, original line, and outdated status. Prefer the current line when available;
make an outdated/original-line fallback visible rather than implying it is exact.
Handle unavailable/deleted files gracefully and preserve TRAMP/worktree context.

Decide during editor integration whether opening happens on selection, when work
starts, or through an explicit action. Keep it opt-in and avoid a dependency on
Emacs for ordinary CLI use.
