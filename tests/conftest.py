"""Fixtures for the Phase 1 tests: real Git repositories, fake `gh`/`codex` on PATH.

Nothing here is autouse, so the Phase 0 spike tests run exactly as before.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from phase1_support import REPO, git, pull_request

TESTS = Path(__file__).resolve().parent


@pytest.fixture
def isolated_env(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep tests away from the user's Git config (e.g. signing) and Codex home."""
    home = tmp_path_factory.mktemp("home")
    config = home / "gitconfig"
    config.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n[init]\n\tdefaultBranch = main\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))
    monkeypatch.delenv("PRFLOW_CODEX_BIN", raising=False)
    return home


@pytest.fixture
def repo(tmp_path: Path, isolated_env: Path) -> Path:
    root = tmp_path / "widgets"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("print('hi')\n")
    git(root, "add", "app.py")
    git(root, "commit", "-q", "-m", "init")
    git(root, "remote", "add", "origin", f"https://github.com/{REPO}.git")
    git(root, "checkout", "-q", "-b", "feature")
    return root


class FakeTools:
    def __init__(self, directory: Path) -> None:
        self.scenario_path = directory / "scenario.json"
        self.gh_log = directory / "gh.log"
        self.codex_log = directory / "codex.log"
        self.scenario: dict[str, Any] = {"repo": REPO, "login": "octo", "prs": [pull_request(7, "feature")]}
        self.save()

    def save(self) -> None:
        self.scenario_path.write_text(json.dumps(self.scenario))

    def pr(self, number: int) -> dict[str, Any]:
        return next(p for p in self.scenario["prs"] if p["number"] == number)

    def calls(self) -> list[list[str]]:
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()]

    def codex_calls(self) -> list[list[str]]:
        if not self.codex_log.exists():
            return []
        return [json.loads(line) for line in self.codex_log.read_text().splitlines()]


def _executable(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_env: Path) -> FakeTools:
    directory = tmp_path / "fake"
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True)
    tools = FakeTools(directory)
    _executable(bin_dir / "gh", f"import runpy, sys\nsys.argv[0] = 'gh'\nrunpy.run_path({str(TESTS / 'fake_gh.py')!r}, run_name='__main__')\n")
    _executable(
        bin_dir / "codex",
        "import json, os, sys\n"
        "open(os.environ['PRFLOW_FAKE_CODEX_LOG'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('codex-cli 0.154.0')\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PRFLOW_FAKE_GH_SCENARIO", str(tools.scenario_path))
    monkeypatch.setenv("PRFLOW_FAKE_GH_LOG", str(tools.gh_log))
    monkeypatch.setenv("PRFLOW_FAKE_CODEX_LOG", str(tools.codex_log))
    return tools


@dataclass
class CliResult:
    code: int
    out: str
    err: str
    data: Any


@pytest.fixture
def run_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from prflow import cli

    def run(cwd: Path, *args: str) -> CliResult:
        monkeypatch.chdir(cwd)
        code = cli.main(list(args))
        out, err = capsys.readouterr()
        return CliResult(code, out, err, json.loads(out) if "--json" in args else None)

    return run
