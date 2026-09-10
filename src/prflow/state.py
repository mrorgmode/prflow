"""Per-worktree workflow state: `<git rev-parse --git-path prflow>/state.json`.

Readers take no lock: the file is only ever replaced atomically. Every
read-modify-write goes through `transaction()`, which holds an exclusive
`fcntl.flock` on `state.lock` for the whole reread/verify/mutate/replace cycle
(SPEC §9.2). Linux/WSL/macOS only; native Windows is not supported.

A corrupted file, a newer schema, or a stale expected revision is an error; prflow
never discards or overwrites state it cannot understand.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import PrflowError

SCHEMA_VERSION = 1
STATE_FILE = "state.json"
LOCK_FILE = "state.lock"


def empty_state() -> dict[str, Any]:
    # branches: local branch name -> {"pr": "OWNER/REPO#N", "bound_at": ...}
    # prs: "OWNER/REPO#N" -> cached PR context, alias counter and review threads
    return {"schema_version": SCHEMA_VERSION, "state_revision": 0, "branches": {}, "prs": {}}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate(data: Any, path: Path) -> dict[str, Any]:
    hint = f"prflow will not overwrite it; inspect or move {path} aside"
    if not isinstance(data, dict) or not _is_int(data.get("schema_version")):
        raise PrflowError("state_corrupt", f"{path} has no integer schema_version", hint=hint)
    if data["schema_version"] > SCHEMA_VERSION:
        raise PrflowError(
            "state_future_schema",
            f"{path} uses schema {data['schema_version']}, newer than this prflow ({SCHEMA_VERSION})",
            hint="upgrade prflow; " + hint,
        )
    if data["schema_version"] < 1:
        raise PrflowError("state_corrupt", f"{path} has invalid schema_version {data['schema_version']}", hint=hint)
    if not _is_int(data.get("state_revision")) or data["state_revision"] < 0:
        raise PrflowError("state_corrupt", f"{path} has no valid state_revision", hint=hint)
    if not isinstance(data.get("branches"), dict) or not isinstance(data.get("prs"), dict):
        raise PrflowError("state_corrupt", f"{path} is missing its branches/prs maps", hint=hint)
    for binding in data["branches"].values():
        if not isinstance(binding, dict) or not isinstance(binding.get("pr"), str):
            raise PrflowError("state_corrupt", f"{path} has an invalid branch binding", hint=hint)
    for entry in data["prs"].values():
        if not isinstance(entry, dict) or not isinstance(entry.get("threads"), dict):
            raise PrflowError("state_corrupt", f"{path} has an invalid PR cache", hint=hint)
        counter = entry.get("next_alias")
        if not _is_int(counter) or counter < 1:
            raise PrflowError("state_corrupt", f"{path} has an invalid alias counter", hint=hint)
        aliases = set()
        for tid, record in entry["threads"].items():
            if not isinstance(record, dict) or record.get("github_thread_id") != tid:
                raise PrflowError("state_corrupt", f"{path} has an invalid thread record", hint=hint)
            alias = record.get("alias")
            if alias is None:
                continue
            if not isinstance(alias, str) or not re.fullmatch(r"T[1-9][0-9]*", alias):
                raise PrflowError("state_corrupt", f"{path} has an invalid review alias", hint=hint)
            if alias in aliases or int(alias[1:]) >= counter:
                raise PrflowError("state_corrupt", f"{path} would reuse a review alias", hint=hint)
            aliases.add(alias)
    return data


def read_state(state_dir: Path) -> dict[str, Any]:
    path = state_dir / STATE_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return empty_state()
    except OSError as exc:
        raise PrflowError("state_unreadable", f"cannot read {path}: {exc}") from None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise PrflowError(
            "state_corrupt", f"{path} is not valid JSON", hint=f"prflow will not overwrite it; inspect or move {path} aside"
        ) from None
    return _validate(data, path)


def _atomic_write(path: Path, state: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


@contextlib.contextmanager
def transaction(state_dir: Path, expected_revision: int | None = None) -> Iterator[dict[str, Any]]:
    """Yield the latest state for mutation; persist it atomically if it changed.

    The revision is incremented only when content changes. After the block, the
    yielded dict carries the new ``state_revision``.
    """
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        lock = open(state_dir / LOCK_FILE, "a+b")
    except OSError as exc:
        raise PrflowError("state_unwritable", f"cannot create prflow state in {state_dir}: {exc}") from None
    with lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = read_state(state_dir)
        revision = state["state_revision"]
        if expected_revision is not None and expected_revision != revision:
            raise PrflowError(
                "state_revision_mismatch",
                f"local state is at revision {revision}, not the expected {expected_revision}",
                hint="re-read state (e.g. `prflow status --json`) and retry",
                details={"expected_revision": expected_revision, "state_revision": revision},
            )
        before = json.dumps(state, sort_keys=True)
        yield state
        state["schema_version"] = SCHEMA_VERSION
        state["state_revision"] = revision
        if json.dumps(state, sort_keys=True) == before:
            return
        state["state_revision"] = revision + 1
        _atomic_write(state_dir / STATE_FILE, state)
