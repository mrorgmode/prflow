"""Local Git facts via `git` subprocesses (no Git library)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import PrflowError

GIT_TIMEOUT = 60


def run_git(args: list[str], cwd: Path | str | None = None, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL
        )
    except FileNotFoundError:
        raise PrflowError("git_missing", "git executable not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise PrflowError("git_failed", f"git {args[0]} timed out after {GIT_TIMEOUT}s") from None
    if check and proc.returncode != 0:
        raise PrflowError("git_failed", f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc


def config_get(key: str, cwd: Path | str) -> str | None:
    proc = run_git(["config", "--get", key], cwd, check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_path(root: Path, name: str) -> Path:
    """`git rev-parse --git-path NAME`, made absolute.

    This honours linked worktrees (`.git` file -> `.git/worktrees/<name>/`) and, for
    `hooks/...`, `core.hooksPath`.
    """
    out = run_git(["rev-parse", "--git-path", name], root).stdout.strip()
    return Path(os.path.normpath(root / out))


@dataclass(frozen=True)
class Checkout:
    root: Path
    state_dir: Path
    branch: str | None  # None when HEAD is detached
    head: str | None  # None in a repository without commits
    upstream_branch: str | None  # branch name from branch.<name>.merge, if configured
    dirty: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "state_dir": str(self.state_dir),
            "branch": self.branch,
            "detached": self.branch is None,
            "head": self.head,
            "upstream_branch": self.upstream_branch,
            "dirty": self.dirty,
        }


def inspect_checkout(cwd: Path | str) -> Checkout:
    proc = run_git(["rev-parse", "--show-toplevel"], cwd, check=False)
    if proc.returncode != 0:
        raise PrflowError(
            "not_a_git_repo",
            f"not inside a Git working tree: {cwd}",
            hint="run prflow from a checkout (or linked worktree) of the pull request's repository",
        )
    root = Path(proc.stdout.strip())
    state_dir = git_path(root, "prflow")

    symref = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], root, check=False)
    branch = symref.stdout.strip() if symref.returncode == 0 else None
    head = run_git(["rev-parse", "--verify", "--quiet", "HEAD"], root, check=False).stdout.strip() or None

    upstream = None
    if branch:
        merge = config_get(f"branch.{branch}.merge", root)
        if merge and merge.startswith("refs/heads/"):
            upstream = merge[len("refs/heads/"):]

    dirty = bool(run_git(["status", "--porcelain"], root).stdout.strip())
    return Checkout(root=root, state_dir=state_dir, branch=branch, head=head, upstream_branch=upstream, dirty=dirty)
