# Future ideas

These are user-requested ideas, not additions to the currently authorized scope.

## Clarify staged review text before publication

Dogfooding feedback, 2026-09-11: after `uv run prflow review show t1`, the user
found the agent-authored fixture comment hard to understand and would have written
it more clearly. This is feedback about the text presented during the test;
Phase 1 does not generate or publish comments.

Future staging/outbox UX could let the user ask Codex to clarify a draft before
uploading it to GitHub. Options to discuss are a preset **Clarify** action, a
free-text instruction such as “explain which behavior this affects,” or handing
the draft to interactive Codex. Determine whether a dedicated action adds value
over the interactive workflow before choosing an interface.

Any revised text should remain staged and visible for review before publication.
Clarification should preserve the intended technical claim; it should not silently
add unsupported claims or publish the revision. No implementation or SPEC change
is implied by this note. See also the [dogfood reflections](phase1-dogfood.md#user-reflections).

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

## Configure Codex model and effort to fit available quota

Requested 2026-09-11: let the user configure the Codex model and reasoning effort
according to remaining quota and the cost/difficulty of the work. A generated
configuration file with initial defaults would be enough as an initial interface.
SPEC.md Draft 0.3 does not currently define this configuration surface.

Start the design discussion with explicit global model/effort defaults. Later,
consider task-specific overrides (for example, triage, fixes, and rewriting staged
text), so simpler work can use a cheaper model while difficult work gets more
capacity. Task categories and defaults still need agreement; automatic routing or
quota detection is not implied by this request.

Decide file location, initialization, precedence, and how the effective settings
are shown before a run. Avoid silently upgrading to a more expensive model when a
configured choice is unavailable. Record this alongside the staged-text UX idea
for future planning; do not add configuration or batch execution in Spike F.

## Optional Claude second opinion on staged work

Requested 2026-09-11: let the user request a second opinion from Claude on a staged
item before publication. prflow would assemble the relevant review context, proposed
changes and staged text, then receive a review in a defined format. Codex could
analyse that feedback and propose revisions to the staged item for human review.

Also consider an opt-in automatic path: Codex draft → Claude second opinion →
Codex assessment/revision → staging. The user describes this as an “auto
second-opinion loop”; whether it means one pass or bounded repeated passes remains
a design question. Neither path should imply automatic GitHub publication.

Open questions include the minimum context bundle, structured response format,
how disagreements and accepted/rejected suggestions are shown, handling reviewer
failure, and retaining the before/after draft. Coordinate model/effort settings
with the quota configuration idea above; agree explicit cost/iteration limits and
stopping rules before adding an automatic loop. The additional reviewer should
not create nested agents by default. No implementation, dependency, or SPEC change
is authorized by this planning note.
