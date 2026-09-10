# prflow

Local-first workflow harness for developing GitHub pull requests with Codex.
See `SPEC.md`. This checkout currently contains **Phase 0 only**: feasibility
spikes, their offline tests, and the spike report in
`docs/spikes/0001-foundation.md`. There is no product code yet.

## Dependencies (as installed on 2026-09-10)

| Component | Version | How it is provided |
|---|---|---|
| Python | 3.14.4 (project requires >= 3.11) | system `python3` |
| uv | 0.12.12 | pre-installed |
| openai-codex (Python SDK) | 0.147.0, pinned | `uv sync` (PyPI latest as of 2026-09-10) |
| openai-codex-cli-bin | 0.147.0 (bundled `codex-cli 0.147.0`) | pulled in by the SDK |
| native `codex` CLI | 0.154.0 standalone | already installed in `~/.local/bin/codex`; used for all live spikes via `PRFLOW_CODEX_BIN` |
| gh | 2.100.0 | system package, authenticated by the user |
| git | 2.53.0 | system package |
| bubblewrap (`bwrap`) | 0.11.1 | installed with `sudo apt-get install -y bubblewrap` during Phase 0 because the Codex app-server logged "could not find bubblewrap on PATH" and fell back to its legacy Linux helper |
| pytest | 9.1.1 | dev dependency group |

## Setup

```bash
uv sync --frozen            # creates .venv from uv.lock (openai-codex==0.147.0)
uv run pytest -q            # offline tests; no Codex or GitHub network needed
```

## Reproducible spike commands

No model turns (runtime and GitHub probes still use network):

```bash
# runtime/protocol probe, batch launch vs SDK-default baseline
PRFLOW_CODEX_BIN=$(readlink -f "$(command -v codex)") uv run python spikes/probe_runtime.py --baseline
# direct sandbox probe, no model
codex sandbox -P :workspace -C "$PWD" -- bash spikes/sandbox_probe.sh
# Spike D read path (any readable PR); mutations stay dry-run without PRFLOW_ALLOW_GITHUB_WRITE=1
uv run python spikes/spike_d_graphql.py --repo openai/codex --pr 35882
# Spike C inherited-MCP control (dummy local server, temporary config only)
PRFLOW_CODEX_BIN=$(readlink -f "$(command -v codex)") uv run python spikes/spike_c_mcp_inherit.py
# Spike E argv + prompt dry run, and real TUI startup smoke in a pty (no prompt, no model turn)
uv run python spikes/spike_e_interactive.py
uv run python spikes/spike_e_interactive.py --smoke
```

Live (each consumes a few short `gpt-5.6-luna`, effort `low` turns; never the account default model):

```bash
export PRFLOW_CODEX_BIN=$(readlink -f "$(command -v codex)")
uv run python spikes/spike_a_structured_turn.py
uv run python spikes/spike_b_untrusted_input.py      # exit 2 = fallback delivery, exploratory only
uv run python spikes/spike_c_isolation.py            # one workspace_write turn; captures and auto-deletes a runtime trace
uv run python spikes/spike_c_toolsurface.py --reanalyze DIR   # only for a trace you deliberately kept
```

Evidence lands in `docs/spikes/evidence/*.json` (bounded, redacted, includes the
exact launch configuration). Raw runtime traces contain model reasoning; the spikes
create them in a temporary directory and delete them in `finally`.

## Status

Phase 0 is documented in `docs/spikes/0001-foundation.md`. Batch mode is
**not yet available under the strict spec** because the published SDK lacks
`ExternalMessage`; see that document for the per-spike status and decisions
that need a human.
