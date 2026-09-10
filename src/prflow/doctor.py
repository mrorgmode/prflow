"""`prflow doctor`: report readiness; change nothing.

Phase 1 (read-only orientation) readiness is reported separately from Phase 2 batch
readiness. Batch readiness is never green in Phase 1: the SDK/runtime pair probe is not
implemented, and Spike F is pending. Doctor runs no model turns, starts no Codex
runtime, reads no credential files, and modifies no Git/Codex/user configuration.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import git, github, review
from . import state as state_store
from .errors import PrflowError

MIN_RUNTIME_FOR_EXTERNAL_MESSAGE = (0, 151, 0)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _check(group: str, name: str, status: str, detail: str, hint: str | None = None) -> dict[str, Any]:
    # status: ok | warn | fail | info | unavailable | unverified | skipped
    return {"group": group, "name": name, "status": status, "detail": detail, "hint": hint}


def _first_line(argv: list[str], cwd: Path | None = None) -> str | None:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout.strip().splitlines() or [""])[0]


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    match = _VERSION_RE.search(text or "")
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def sdk_info() -> dict[str, Any]:
    """Installed openai-codex facts from package metadata plus one attribute check.

    Importing the Python package does not start a Codex runtime.
    """
    info: dict[str, Any] = {"installed": False, "version": None, "bundled_runtime": None, "external_message": None}
    try:
        info["version"] = importlib.metadata.version("openai-codex")
    except importlib.metadata.PackageNotFoundError:
        return info
    info["installed"] = True
    try:
        spec = importlib.util.find_spec("codex_cli_bin")
    except (ImportError, ValueError):
        spec = None
    if spec and spec.submodule_search_locations:
        package_json = Path(list(spec.submodule_search_locations)[0]) / "codex-package.json"
        try:
            info["bundled_runtime"] = json.loads(package_json.read_text())["version"]
        except (OSError, ValueError, KeyError):
            pass
    try:
        module = importlib.import_module("openai_codex")
        info["external_message"] = hasattr(module, "ExternalMessage")
    except Exception as exc:  # report, never crash doctor
        info["import_error"] = f"{type(exc).__name__}: {exc}"[:200]
    return info


def _phase1_checks(cwd: Path, repo_arg: str | None, pr_arg: int | None) -> tuple[list[dict[str, Any]], git.Checkout | None]:
    group = "phase1"
    checks = []
    git_version = _first_line(["git", "--version"])
    checks.append(
        _check(group, "git", "ok", git_version)
        if git_version
        else _check(group, "git", "fail", "git not found on PATH", "install git")
    )
    checkout = None
    if git_version:
        try:
            checkout = git.inspect_checkout(cwd)
            where = checkout.branch or f"detached at {(checkout.head or '')[:12]}"
            checks.append(_check(group, "checkout", "ok", f"{checkout.root} ({where})"))
        except PrflowError as exc:
            checks.append(_check(group, "checkout", "fail", exc.message, exc.hint))
    if checkout:
        try:
            st = state_store.read_state(checkout.state_dir)
            exists = (checkout.state_dir / state_store.STATE_FILE).exists()
            detail = f"{checkout.state_dir} (revision {st['state_revision']})" if exists else f"{checkout.state_dir} (not created yet)"
            nearest = checkout.state_dir if checkout.state_dir.exists() else checkout.state_dir.parent
            if os.access(nearest, os.W_OK):
                checks.append(_check(group, "state", "ok", detail))
            else:
                checks.append(_check(group, "state", "fail", f"{nearest} is not writable"))
        except PrflowError as exc:
            checks.append(_check(group, "state", "fail", exc.message, exc.hint))

    gh_version = _first_line(["gh", "--version"])
    if not gh_version:
        checks.append(_check(group, "gh", "fail", "GitHub CLI `gh` not found", "install it from https://cli.github.com"))
        return checks, checkout
    checks.append(_check(group, "gh", "ok", gh_version))
    checks.append(_gh_auth_check(cwd))
    if checkout is None or checks[-1]["status"] == "fail":
        checks.append(_check(group, "pull_request", "skipped", "needs a checkout and gh authentication"))
        return checks, checkout
    try:
        repo, number, how = review.resolve_target(checkout, repo_arg, pr_arg)
        meta = github.fetch_pr(repo, number, checkout.root)
        via = "current branch" if how == "branch" else "--pr"
        checks.append(_check(group, "pull_request", "ok", f"{repo}#{number} via {via}: {meta.get('state')}, {meta.get('title')!r}"))
    except PrflowError as exc:
        # Being on a branch without a PR is normal; status/review accept --pr.
        soft = exc.code in ("no_pr", "detached_head", "pr_ambiguous", "pr_not_found")
        checks.append(_check(group, "pull_request", "warn" if soft else "fail", f"[{exc.code}] {exc.message}", exc.hint))
    return checks, checkout


def _gh_auth_check(cwd: Path) -> dict[str, Any]:
    try:
        data = github.gh_json(["auth", "status", "--json", "hosts"], cwd)
    except PrflowError as exc:
        return _check("phase1", "gh_auth", "fail", exc.message, "run `gh auth login`")
    active = [
        f"{host}: {account.get('login')}"
        for host, accounts in (data.get("hosts") or {}).items()
        for account in accounts or []
        if account.get("active") and account.get("state") == "success"
    ]
    if not active:
        return _check("phase1", "gh_auth", "fail", "no active authenticated gh account", "run `gh auth login`")
    return _check("phase1", "gh_auth", "ok", ", ".join(active))


def _interactive_checks() -> tuple[list[dict[str, Any]], str | None]:
    group = "interactive"
    path = shutil.which("codex")
    version = _first_line([path, "--version"]) if path else None
    if not version:
        return [_check(group, "native_codex", "warn", "native `codex` CLI not found", "needed later for --interactive; not used by Phase 1")], None
    checks = [_check(group, "native_codex", "ok", f"{version} ({path})")]
    auth = codex_home() / "auth.json"
    checks.append(
        _check(group, "codex_login", "info", f"{auth} present (not read)")
        if auth.is_file()
        else _check(group, "codex_login", "info", f"no {auth}; Codex may use another login method")
    )
    return checks, version


def _batch_checks(native_version: str | None, sdk: dict[str, Any]) -> list[dict[str, Any]]:
    group = "batch"
    checks = []
    if not sdk["installed"]:
        checks.append(
            _check(group, "sdk", "unavailable", "openai-codex is not installed", "optional for Phase 1: pip install 'prflow[codex]'")
        )
    else:
        bundled = sdk["bundled_runtime"]
        detail = f"openai-codex {sdk['version']}, bundled runtime {bundled or 'unknown'}"
        native = parse_version(native_version)
        if native and bundled and native != parse_version(bundled):
            detail += f"; native CLI is {'.'.join(map(str, native))} (independent; do not treat as the batch runtime)"
        checks.append(_check(group, "sdk", "info", detail))
        if "import_error" in sdk:
            checks.append(_check(group, "external_message", "unavailable", f"openai_codex failed to import: {sdk['import_error']}"))
        elif sdk["external_message"]:
            checks.append(_check(group, "external_message", "ok", f"openai-codex {sdk['version']} exports ExternalMessage"))
        else:
            checks.append(
                _check(
                    group,
                    "external_message",
                    "unavailable",
                    f"openai-codex {sdk['version']} does not export ExternalMessage (tool-authority input, SPEC §5.5)",
                    "wait for a published SDK that exports it; the user-role fallback does not count",
                )
            )
        runtime = parse_version(bundled)
        minimum = ".".join(map(str, MIN_RUNTIME_FOR_EXTERNAL_MESSAGE))
        if runtime is None:
            checks.append(_check(group, "runtime_version", "unavailable", "SDK-bundled runtime version unknown"))
        elif runtime < MIN_RUNTIME_FOR_EXTERNAL_MESSAGE:
            checks.append(_check(group, "runtime_version", "unavailable", f"bundled runtime {bundled} < {minimum} required for ExternalMessage"))
        else:
            checks.append(_check(group, "runtime_version", "ok", f"bundled runtime {bundled} >= {minimum}"))
    if os.environ.get("PRFLOW_CODEX_BIN"):
        checks.append(
            _check(group, "runtime_override", "warn", "PRFLOW_CODEX_BIN is set: a development spike override, not a supported pairing")
        )
    checks.append(
        _check(
            group,
            "pair_probe",
            "unverified",
            "SDK/runtime pair feature probe is not implemented in Phase 1, so batch readiness is never reported green",
        )
    )
    checks.append(
        _check(
            group,
            "credential_read",
            "warn",
            "Phase 0 observed a sandboxed batch command reading ~/.config/gh/hosts.yml; "
            "Spike F (credential-read isolation, required before Phase 2) is pending. Not re-probed here.",
        )
    )
    return checks


def _plugin_checks() -> list[dict[str, Any]]:
    group = "github_plugin"
    cache = codex_home() / "plugins" / "cache"
    manifests = sorted(cache.glob("*/github/*/.codex-plugin/plugin.json"), key=lambda p: p.stat().st_mtime)
    if not manifests:
        return [_check(group, "github_plugin", "info", f"no GitHub plugin in {cache}; only interactive Codex would use it")]
    plugin_dir = manifests[-1].parent.parent
    try:
        manifest = json.loads(manifests[-1].read_text()[:1_000_000])
    except (OSError, ValueError):
        manifest = {}
    skills = sorted(p.parent.name for p in plugin_dir.glob("skills/*/SKILL.md"))
    connector = (plugin_dir / ".app.json").exists()
    return [
        _check(
            group,
            "github_plugin",
            "info",
            f"{manifest.get('name', 'github')} {manifest.get('version', plugin_dir.name)} cached under {plugin_dir.parent.parent.name}"
            + ("; connector app manifest present" if connector else "")
            + "; interactive Codex only, never loaded in batch (SPEC §6.2)",
        ),
        _check(
            group,
            "skill_payload",
            "info",
            ("local skill payloads: " + ", ".join(skills)) if skills else "no local skill payload (gh-address-comments not available as a file)",
        ),
    ]


def _repository_checks(checkout: git.Checkout) -> list[dict[str, Any]]:
    group = "repository"
    root = checkout.root
    checks = []
    config = root / ".prflow.toml"
    if not config.exists():
        checks.append(_check(group, "checks_config", "info", "no .prflow.toml check configuration"))
    else:
        try:
            sets = tomllib.loads(config.read_text()).get("checks", {})
            valid = isinstance(sets, dict) and all(
                isinstance(cmds, list) and all(isinstance(c, list) and c and all(isinstance(a, str) for a in c) for c in cmds)
                for cmds in sets.values()
            )
            if not valid:
                raise ValueError("[checks] entries must be lists of argv arrays")
            listed = ", ".join(f"{name} ({len(cmds)} command{'s' if len(cmds) != 1 else ''})" for name, cmds in sets.items())
            checks.append(_check(group, "checks_config", "info", f"check sets: {listed or 'none'} (not run in Phase 1)"))
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            checks.append(_check(group, "checks_config", "warn", f".prflow.toml is invalid: {exc}"))

    if (root / ".pre-commit-config.yaml").exists():
        hook = git.git_path(root, "hooks/pre-commit")
        installed = hook.is_file() and os.access(hook, os.X_OK)
        tool = "pre-commit on PATH" if shutil.which("pre-commit") else "pre-commit not on PATH"
        checks.append(_check(group, "pre_commit", "info", f".pre-commit-config.yaml present; {tool}; hook {'installed' if installed else 'not installed'}"))
    else:
        checks.append(_check(group, "pre_commit", "info", "no .pre-commit-config.yaml"))

    gpgsign = git.config_get("commit.gpgsign", root)
    signingkey = git.config_get("user.signingkey", root)
    fmt = git.config_get("gpg.format", root)
    detail = f"commit.gpgsign={gpgsign or 'unset'}, gpg.format={fmt or 'unset (openpgp)'}, user.signingkey={'set' if signingkey else 'unset'}"
    if (gpgsign or "").lower() == "true" and not signingkey:
        checks.append(_check(group, "signing", "warn", detail + "; signing requested without a key", "prflow never changes signing settings"))
    else:
        suffix = "; signing appears configured" if (gpgsign or "").lower() == "true" else ""
        checks.append(_check(group, "signing", "info", detail + suffix))
    return checks


def _sandbox_checks() -> list[dict[str, Any]]:
    if not sys.platform.startswith("linux"):
        return [_check("sandbox", "bubblewrap", "info", f"{sys.platform}: sandbox backend not checked")]
    path = shutil.which("bwrap")
    version = _first_line([path, "--version"]) if path else None
    if not version:
        return [_check("sandbox", "bubblewrap", "warn", "bwrap not found; Codex would fall back to its legacy Linux sandbox (Phase 2 batch)")]
    return [_check("sandbox", "bubblewrap", "ok", f"{version} ({path})")]


def run(cwd: Path, repo_arg: str | None = None, pr_arg: int | None = None) -> dict[str, Any]:
    checks, checkout = _phase1_checks(cwd, repo_arg, pr_arg)
    interactive, native_version = _interactive_checks()
    checks += interactive
    checks += _batch_checks(native_version, sdk_info())
    checks += _plugin_checks()
    if checkout:
        checks += _repository_checks(checkout)
    checks += _sandbox_checks()

    phase1_ready = not any(c["status"] == "fail" for c in checks if c["group"] == "phase1")
    batch = [c for c in checks if c["group"] == "batch"]
    return {
        "summary": {
            "phase1_ready": phase1_ready,
            "interactive": "available" if native_version else "unavailable",
            "batch": "unavailable" if any(c["status"] == "unavailable" for c in batch) else "unverified",
            "batch_reasons": [c["detail"] for c in batch if c["status"] in ("unavailable", "unverified")],
        },
        "checks": checks,
    }
