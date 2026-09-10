"""Spike C helper: re-analyze a *kept* raw rollout trace directory into sanitized tool metadata.

The live capture now lives in spike_c_isolation.py (one turn, trace deleted automatically).
Use this only if you deliberately kept a trace with CODEX_ROLLOUT_TRACE_ROOT yourself:

    uv run python spikes/spike_c_toolsurface.py --reanalyze /path/to/trace-root

Exit status is 1 unless the runtime ALL_TOOLS output was actually captured (no vacuous passes).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from batch_launch import write_evidence  # noqa: E402
from rollout_trace import analyze, surface_checks  # noqa: E402


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--reanalyze":
        print(__doc__)
        return 2
    summary = analyze(Path(sys.argv[2]))
    checks = surface_checks(summary.all_names(), summary.captured)
    evidence = {**summary.as_dict(), "checks": checks, "reanalyzed_from": sys.argv[2]}
    path = write_evidence("spike_c_toolsurface.json", evidence)
    print(json.dumps({"checks": checks, "runtime_ALL_TOOLS": summary.runtime_all_tools, "evidence": str(path)}, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
