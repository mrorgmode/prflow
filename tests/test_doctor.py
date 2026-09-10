"""`prflow doctor`: honest Phase 1 vs. batch readiness, no side effects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from phase1_support import git
from prflow import doctor

pytestmark = pytest.mark.usefixtures("isolated_env")

PINNED_SDK = {"installed": True, "version": "0.147.0", "bundled_runtime": "0.147.0", "external_message": False}


def checks_by_name(data: dict) -> dict[str, dict]:
    return {c["name"]: c for c in data["checks"]}


def test_phase1_ready_while_batch_is_unavailable(repo, fake_gh, run_cli, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "sdk_info", lambda: dict(PINNED_SDK))
    r = run_cli(repo, "doctor", "--json")
    assert r.code == 0 and r.data["ok"] is True
    summary = r.data["summary"]
    assert summary["phase1_ready"] is True and summary["batch"] == "unavailable" and summary["interactive"] == "available"
    checks = checks_by_name(r.data)
    assert checks["pull_request"]["status"] == "ok" and "acme/widgets#7" in checks["pull_request"]["detail"]
    assert checks["gh_auth"]["detail"] == "github.com: octo"
    assert checks["external_message"]["status"] == "unavailable"
    assert checks["runtime_version"]["status"] == "unavailable"
    assert checks["pair_probe"]["status"] == "unverified"
    assert checks["credential_read"]["status"] == "warn" and "Spike F" in checks["credential_read"]["detail"]
    assert "native CLI is 0.154.0" in checks["sdk"]["detail"]
    assert fake_gh.codex_calls() == [["--version"]], "doctor only asks the native CLI for its version"
    assert not (repo / ".git" / "prflow").exists(), "doctor writes no state"
    assert not any("mutation" in " ".join(call) for call in fake_gh.calls())

    human = run_cli(repo, "doctor")
    assert "Phase 1 read-only orientation: READY" in human.out and "batch triage/fix (Phase 2): UNAVAILABLE" in human.out


def test_batch_is_never_green_in_phase1_even_with_external_message(repo, fake_gh, run_cli, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "sdk_info", lambda: {**PINNED_SDK, "version": "0.160.0", "bundled_runtime": "0.160.0", "external_message": True})
    r = run_cli(repo, "doctor", "--json")
    checks = checks_by_name(r.data)
    assert checks["external_message"]["status"] == "ok" and checks["runtime_version"]["status"] == "ok"
    assert r.data["summary"]["batch"] == "unverified"


def test_missing_sdk_does_not_affect_phase1(repo, fake_gh, run_cli, monkeypatch) -> None:
    monkeypatch.setattr(doctor, "sdk_info", lambda: {"installed": False, "version": None, "bundled_runtime": None, "external_message": None})
    r = run_cli(repo, "doctor", "--json")
    assert r.code == 0 and r.data["summary"]["phase1_ready"] is True
    assert checks_by_name(r.data)["sdk"]["status"] == "unavailable"


def test_real_sdk_probe_reports_the_pinned_package_honestly() -> None:
    info = doctor.sdk_info()
    if not info["installed"]:
        pytest.skip("openai-codex not installed")
    if info["version"] == "0.147.0":
        assert info["external_message"] is False and info["bundled_runtime"] == "0.147.0"


@pytest.mark.parametrize(
    ("change", "failing"),
    [
        (lambda fake: fake.scenario.update(authenticated=False), "gh_auth"),
        (lambda fake: fake.scenario.update(fail={"repo view": {"stderr": "HTTP 500: boom"}}), "pull_request"),
    ],
    ids=["unauthenticated", "github-down"],
)
def test_github_problems_make_phase1_not_ready(repo, fake_gh, run_cli, change, failing) -> None:
    change(fake_gh)
    fake_gh.save()
    r = run_cli(repo, "doctor", "--json")
    assert r.code == 1 and r.data["ok"] is False and r.data["summary"]["phase1_ready"] is False
    assert checks_by_name(r.data)[failing]["status"] == "fail"


def test_branch_without_pr_is_only_a_warning(repo, fake_gh, run_cli) -> None:
    fake_gh.scenario["prs"] = []
    fake_gh.save()
    r = run_cli(repo, "doctor", "--json")
    assert r.code == 0 and checks_by_name(r.data)["pull_request"]["status"] == "warn"


def test_outside_a_repository_is_not_ready(tmp_path, fake_gh, run_cli) -> None:
    outside = tmp_path / "nowhere"
    outside.mkdir()
    r = run_cli(outside, "doctor", "--json")
    assert r.code == 1 and checks_by_name(r.data)["checkout"]["status"] == "fail"


def test_plugin_metadata_and_repository_policy_are_reported_without_changes(repo, fake_gh, run_cli, isolated_env: Path) -> None:
    plugin = isolated_env / "codex" / "plugins" / "cache" / "curated" / "github" / "0.1.12-abc"
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "github", "version": "0.1.12-abc"}))
    (plugin / ".app.json").write_text("{}")
    (repo / ".prflow.toml").write_text('schema_version = 1\n[checks]\ndefault = [["uv", "run", "pytest"]]\n')
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    git(repo, "config", "commit.gpgsign", "true")

    r = run_cli(repo, "doctor", "--json")
    checks = checks_by_name(r.data)
    assert "github 0.1.12-abc" in checks["github_plugin"]["detail"] and "connector" in checks["github_plugin"]["detail"]
    assert "no local skill payload" in checks["skill_payload"]["detail"]
    assert "default (1 command)" in checks["checks_config"]["detail"]
    assert "hook not installed" in checks["pre_commit"]["detail"]
    assert checks["signing"]["status"] == "warn"
    assert git(repo, "config", "commit.gpgsign") == "true", "doctor never changes signing configuration"
