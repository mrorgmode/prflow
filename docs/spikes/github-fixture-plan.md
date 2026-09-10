# Proposed GitHub Phase 0 fixture

Repository: https://github.com/mrorgmode/prflow

1. Seed empty `main` with unchanged SPEC.md only (commit 6165aada5c2f49b389f62f308e0cf9abdba35a3a).
2. Push `spike/phase0-review-fixture` (commit ae82ca2dfec7519624c683a3839e5d972cd5cd67).
3. Open a draft PR into main, titled `Phase 0: disposable review-thread fixture`.

PR body:

Codex: Disposable test PR for SPEC.md section 7, Spike D. This fixture verifies deterministic review-thread reads, replies, and resolution. No production implementation is included. Leave unmerged for inspection.

4. Add one inline review comment on spikes/fixtures/review_target.py, line 4:

Codex: Phase 0 test review: please explain whether this greeting helper intentionally omits punctuation. No code change is requested; this thread is a disposable API fixture.

5. Fetch the unresolved thread through GraphQL, then post this exact reply:

Codex: Phase 0 test reply: punctuation is intentionally omitted in this disposable fixture. This reply verifies the deterministic review-thread API; no production change is implied.

6. Refetch, resolve that test thread, and refetch to verify resolution. Leave the draft PR unmerged and branches intact.

Added fixture:
```python
"""Disposable Phase 0 review-thread fixture; not product code."""

def greeting(name: str) -> str:
    return f"Hello, {name}"
```

## Execution

Approved by the user and executed on 2026-09-10. Draft PR: https://github.com/mrorgmode/prflow/pull/1. The test thread received its approved reply and was resolved after a refresh; see evidence/spike_d_live.json. Do not rerun the mutations.
