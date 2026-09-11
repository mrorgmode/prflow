"""In-sandbox credential-read probe for Spike F (stdlib only, run with ``python3 -I -S``).

Usage: ``python3 -I -S spike_f_probe.py '<json spec>'`` where the spec is
``{"targets": [{"id": str, "path": str, "kind": "file"|"dir", "subprocess": bool}],
"workspace_ops": bool}``.

The probe never prints, hashes or keeps file contents. For a file target it opens
the path, reads at most ONE byte, discards it, and reports only a status word:

* ``READABLE``        one byte was read
* ``READABLE_EMPTY``  open succeeded but the file is empty (e.g. a masked placeholder)
* ``BLOCKED``         EACCES/EPERM (a permission refusal, not absence)
* ``MISSING``         ENOENT/ENOTDIR
* ``ERROR:<ERRNO>``   anything else

For a directory target it reports whether the directory can be listed (entry
count only, never names) and whether it can be traversed (``stat`` of ``.``).

Output is bracketed by BEGIN/END sentinel lines so that a sandbox that failed to
start, or a probe that crashed part way, can never be mistaken for BLOCKED.

Spike F.1 (opt-in spec keys, v1 specs behave as before):

* ``"write_targets": [{"id", "path", "op": "append"|"create"|"mkdir"}]`` checks write
  permission without changing content: ``append`` opens an existing file for writing and
  closes it without writing a byte; ``create``/``mkdir`` make a new entry and remove it at
  once. Status words: ``WRITABLE``, ``BLOCKED`` (EACCES/EPERM), ``READONLY`` (EROFS),
  ``MISSING``, ``ERROR:*``.
* ``"git": true`` runs read-only Git commands, one Git config write attempt (removed again
  if it succeeds) and reports the absolute metadata paths Git resolves.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys

BEGIN = "PRFLOW_SPIKE_F_PROBE_BEGIN"
END = "PRFLOW_SPIKE_F_PROBE_END"
PROBE_VERSION = 2

GIT_PATH_KEYS = ("toplevel", "git_dir", "common_dir", "git_path_prflow")
GIT_PATH_ARGS = ("rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir", "--git-path", "prflow")
GIT_READ_OPS = (
    ("git_status", ["git", "status", "--porcelain"]),
    ("git_log", ["git", "log", "-1", "--format=%h"]),
    ("git_diff", ["git", "diff", "--stat"]),
    ("git_diff_cached", ["git", "diff", "--cached", "--stat"]),
    ("git_show_head", ["git", "show", "--stat", "HEAD"]),
    ("git_rev_parse", ["git", *GIT_PATH_ARGS]),
)


def _errno_status(exc: OSError) -> str:
    if exc.errno in (errno.EACCES, errno.EPERM):
        return "BLOCKED"
    if exc.errno in (errno.ENOENT, errno.ENOTDIR):
        return "MISSING"
    return f"ERROR:{errno.errorcode.get(exc.errno or 0, exc.errno)}"


def read_one_byte(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        return _errno_status(exc)
    try:
        if os.path.isdir(path):
            return "ERROR:EISDIR"
        try:
            got = len(os.read(fd, 1))  # length only; the byte is discarded immediately
        except OSError as exc:
            return _errno_status(exc)
    finally:
        os.close(fd)
    return "READABLE" if got else "READABLE_EMPTY"


def subprocess_read(path: str) -> str:
    """Same one-byte read via a child executable; stdout is discarded, stderr only classified."""
    try:
        proc = subprocess.run(
            ["head", "-c", "1", path], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ERROR:{type(exc).__name__}"
    if proc.returncode == 0:
        return "EXIT0"
    err = proc.stderr.decode("utf-8", "replace").lower()
    if "permission denied" in err or "operation not permitted" in err:
        return "BLOCKED"
    if "no such file" in err or "not a directory" in err:
        return "MISSING"
    return f"ERROR:exit{proc.returncode}"


def list_dir(path: str) -> dict[str, object]:
    out: dict[str, object] = {}
    try:
        out["list"] = "LISTABLE"
        out["entries"] = len(os.listdir(path))
    except OSError as exc:
        out["list"] = _errno_status(exc)
    try:
        os.stat(os.path.join(path, "."))
        out["traverse"] = "OK"
    except OSError as exc:
        out["traverse"] = _errno_status(exc)
    return out


def probe_target(target: dict[str, object]) -> dict[str, object]:
    path = str(target["path"])
    row: dict[str, object] = {"id": target["id"], "kind": target.get("kind", "file")}
    try:
        os.lstat(path)
        row["lstat"] = "OK"
    except OSError as exc:
        row["lstat"] = _errno_status(exc)
    if target.get("kind") == "dir":
        row.update(list_dir(path))
    else:
        row["read"] = read_one_byte(path)
        if target.get("subprocess"):
            row["subprocess_read"] = subprocess_read(path)
    return row


def workspace_ops() -> dict[str, str]:
    """Normal repository work that must keep working under the protected profile."""
    ops: dict[str, str] = {}
    name = ".prflow_spike_f_write_test"
    try:
        with open(name, "w", encoding="utf-8") as fh:
            fh.write("ok\n")
        with open(name, encoding="utf-8") as fh:
            ops["write_read_back"] = "OK" if fh.read() == "ok\n" else "MISMATCH"
        os.remove(name)
    except OSError as exc:
        ops["write_read_back"] = _errno_status(exc)
    for label, argv in (
        ("git_status", ["git", "status", "--porcelain"]),
        ("git_log", ["git", "log", "-1", "--format=%h"]),
        ("python_child", [sys.executable, "-I", "-S", "-c", "import json"]),
        ("read_repo_file", ["head", "-c", "1", "README.md"]),
    ):
        ops[label] = _run_status(argv)
    return ops


def _run_status(argv: list[str]) -> str:
    try:
        proc = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        return "OK" if proc.returncode == 0 else f"EXIT{proc.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"ERROR:{type(exc).__name__}"


def _write_status(exc: OSError) -> str:
    if exc.errno == errno.EROFS:
        return "READONLY"
    if exc.errno == errno.EEXIST:
        return "ERROR:EEXIST"  # a create probe must target a fresh name; never read as blocked
    return _errno_status(exc)


def write_attempt(target: dict[str, object]) -> str:
    """Write-permission check that leaves content unchanged (see module doc)."""
    path, op = str(target["path"]), target.get("op")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        if op == "append":
            os.close(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOCTTY | cloexec))  # no byte written
            return "WRITABLE"
        if op == "create":
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | cloexec, 0o600))
            remove = os.unlink
        elif op == "mkdir":
            os.mkdir(path, 0o700)
            remove = os.rmdir
        else:
            return "ERROR:BAD_OP"
    except OSError as exc:
        return _write_status(exc)
    try:
        remove(path)
    except OSError:
        return "WRITABLE_NOT_REMOVED"
    return "WRITABLE"


def git_checks() -> dict[str, object]:
    """Read-only Git commands, one config write attempt, and the metadata paths Git resolves."""
    read = {label: _run_status(argv) for label, argv in GIT_READ_OPS}
    config_set = _run_status(["git", "config", "--local", "prflow.f1probe", "1"])
    if config_set == "OK":  # only possible where metadata is writable (positive control)
        _run_status(["git", "config", "--local", "--remove-section", "prflow"])
    paths: object = None
    try:
        proc = subprocess.run(["git", *GIT_PATH_ARGS], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
        lines = proc.stdout.splitlines()
        if proc.returncode == 0 and len(lines) == len(GIT_PATH_KEYS):
            paths = dict(zip(GIT_PATH_KEYS, lines))
    except (OSError, subprocess.SubprocessError):
        pass
    return {"git_ops": read, "git_write_ops": {"git_config_set": config_set}, "git_paths": paths}


def network_check() -> str:
    """Spike C invariant must survive the profile: direct TCP egress stays refused."""
    import socket

    try:
        with socket.create_connection(("140.82.112.3", 443), timeout=3):
            return "CONNECTED"
    except OSError as exc:
        return _errno_status(exc) if exc.errno else f"ERROR:{type(exc).__name__}"


def main(argv: list[str]) -> int:
    spec = json.loads(argv[1])
    print(BEGIN, flush=True)
    result: dict[str, object] = {
        "probe_version": PROBE_VERSION,
        "uid": os.getuid(),
        "targets": [probe_target(t) for t in spec.get("targets", [])],
    }
    if "write_targets" in spec:
        result["write_targets"] = [{"id": w["id"], "op": w.get("op"), "write": write_attempt(w)} for w in spec["write_targets"]]
    if spec.get("workspace_ops"):
        result["workspace_ops"] = workspace_ops()
    if spec.get("git"):
        result.update(git_checks())
    if spec.get("network"):
        result["network"] = network_check()
    print(json.dumps(result, sort_keys=True), flush=True)
    print(END, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
