"""Unit tests for :mod:`agentic_core.bootstrap.preflight` (U1 / R6 / SC4).

Preflight is pure validation with NO side effects: it checks that every
credential, CLI login, token, and the admin keystore file are present and
usable *before* the orchestrator touches any external system. A failure
must be structural (nothing was created), so the orchestrator routes it
through ``_fail(..., retryable=False)``.

These tests monkeypatch ``shutil.which`` / ``subprocess.run`` / env so no
real ``gh`` / ``railway`` binary is invoked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_core.bootstrap import preflight


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_proc(returncode=0, stdout="", stderr=""):
    """A stand-in for ``subprocess.CompletedProcess``."""
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _make_keystore(tmp_path) -> Path:
    ks = tmp_path / "admin.keystore.json"
    ks.write_text('{"crypto": {}, "version": 3}')
    return ks


def _all_good(monkeypatch, tmp_path):
    """Set up an environment where every preflight check passes."""
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(preflight.subprocess, "run", lambda *a, **k: _ok_proc(0))
    monkeypatch.setenv("RAILWAY_API_TOKEN", "rw-token")
    monkeypatch.setenv("LOGTO_M2M_APP_ID", "m2m-admin-id")
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://auth.example.dev")
    monkeypatch.setenv("PLATFORM_DOMAIN", "example.dev")
    return _make_keystore(tmp_path)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_all_creds_present_passes(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "success"


def test_preflight_success_uses_success_status(monkeypatch, tmp_path):
    """Fix 13: the success shape must use ``status == "success"`` — the same
    value the rest of the create-app contract uses — NOT an off-schema
    ``"passed"`` third status value."""
    ks = _all_good(monkeypatch, tmp_path)
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "success"
    assert result["status"] != "passed"


# ---------------------------------------------------------------------------
# Binary presence
# ---------------------------------------------------------------------------


def test_missing_railway_binary_fails(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    # railway binary absent; everything else present
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: None if name == "railway" else f"/usr/bin/{name}",
    )
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_RAILWAY_MISSING"


def test_missing_gh_binary_fails(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: None if name == "gh" else f"/usr/bin/{name}",
    )
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_GH_MISSING"


def test_missing_railway_binary_does_not_run_auth_check(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: None if name == "railway" else f"/usr/bin/{name}",
    )

    def spy_run(argv, *a, **k):
        calls.append(argv)
        return _ok_proc(0)

    monkeypatch.setattr(preflight.subprocess, "run", spy_run)
    preflight.run_preflight(keystore_path=ks)
    # railway whoami must never be shelled out when the binary is missing
    assert not any("railway" in (argv[0] if argv else "") for argv in calls)


# ---------------------------------------------------------------------------
# Auth checks
# ---------------------------------------------------------------------------


def test_gh_logged_out_fails_with_auth_code(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)

    def run(argv, *a, **k):
        if argv[:1] == ["gh"]:
            return _ok_proc(1, stderr="You are not logged into any GitHub hosts")
        return _ok_proc(0)

    monkeypatch.setattr(preflight.subprocess, "run", run)
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_GH_AUTH_FAILED"


def test_railway_logged_out_fails_with_auth_code(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)

    def run(argv, *a, **k):
        if argv[:1] == ["railway"]:
            return _ok_proc(1, stderr="Unauthorized. Please run railway login")
        return _ok_proc(0)

    monkeypatch.setattr(preflight.subprocess, "run", run)
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_RAILWAY_AUTH_FAILED"


def test_gh_auth_timeout_fails(monkeypatch, tmp_path):
    import subprocess

    ks = _all_good(monkeypatch, tmp_path)

    def run(argv, *a, **k):
        if argv[:1] == ["gh"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=5)
        return _ok_proc(0)

    monkeypatch.setattr(preflight.subprocess, "run", run)
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_GH_AUTH_FAILED"


# ---------------------------------------------------------------------------
# Env vars
# ---------------------------------------------------------------------------


def test_missing_railway_api_token_fails(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_ENV_MISSING"


def test_missing_logto_m2m_app_id_fails(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("LOGTO_M2M_APP_ID", raising=False)
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_ENV_MISSING"


def test_admin_app_id_param_satisfies_logto_env(monkeypatch, tmp_path):
    """An explicit admin_app_id substitutes for the LOGTO_M2M_APP_ID env var."""
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("LOGTO_M2M_APP_ID", raising=False)
    result = preflight.run_preflight(keystore_path=ks, admin_app_id="explicit-id")
    assert result["status"] == "success"


def test_no_logto_endpoint_env_derives_default_from_platform_domain(monkeypatch, tmp_path):
    """P2: preflight must not be stricter than the orchestrator. With no
    LOGTO_ENDPOINT env and no explicit endpoint, preflight derives the SAME
    ``https://auth.<platform_domain>`` default the orchestrator uses — so a run
    driven purely by ``--platform-domain`` is NOT rejected up front."""
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("LOGTO_ENDPOINT", raising=False)
    result = preflight.run_preflight(keystore_path=ks, platform_domain="foo.dev")
    assert result["status"] == "success", result


def test_logto_run_without_platform_domain_fails(monkeypatch, tmp_path):
    """There is no built-in domain. A Logto run that names none fails before
    anything is created, with a message that says how to fix it."""
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("PLATFORM_DOMAIN", raising=False)
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_ENV_MISSING"
    assert result["exit_code"] == preflight.EXIT_MISCONFIG
    assert "--platform-domain" in result["error_message"]


def test_platform_domain_param_satisfies_the_domain_check(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("PLATFORM_DOMAIN", raising=False)
    result = preflight.run_preflight(keystore_path=ks, platform_domain="mine.dev")
    assert result["status"] == "success", result


def test_whitespace_platform_domain_counts_as_missing(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("PLATFORM_DOMAIN", raising=False)
    result = preflight.run_preflight(keystore_path=ks, platform_domain="   ")
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_ENV_MISSING"


def test_no_logto_run_needs_no_platform_domain(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("PLATFORM_DOMAIN", raising=False)
    result = preflight.run_preflight(keystore_path=ks, with_logto=False)
    assert result["status"] == "success", result


# ---------------------------------------------------------------------------
# Keystore
# ---------------------------------------------------------------------------


def test_missing_keystore_fails(monkeypatch, tmp_path):
    _all_good(monkeypatch, tmp_path)
    missing = tmp_path / "does-not-exist.keystore.json"
    result = preflight.run_preflight(keystore_path=missing)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_KEYSTORE_MISSING"


def test_unreadable_keystore_fails(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    # Simulate an unreadable file by chmod 000 (skip if running as root)
    import os

    if os.geteuid() == 0:
        pytest.skip("cannot simulate unreadable file as root")
    ks.chmod(0o000)
    try:
        result = preflight.run_preflight(keystore_path=ks)
    finally:
        ks.chmod(0o600)
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_KEYSTORE_MISSING"


# ---------------------------------------------------------------------------
# Flag-respect: skipped providers don't demand their creds
# ---------------------------------------------------------------------------


def test_no_railway_skips_railway_checks(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    # railway binary absent AND token absent — but with_railway=False so no fail
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: None if name == "railway" else f"/usr/bin/{name}",
    )
    result = preflight.run_preflight(keystore_path=ks, with_railway=False)
    assert result["status"] == "success"


def test_no_github_skips_gh_checks(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: None if name == "gh" else f"/usr/bin/{name}",
    )
    result = preflight.run_preflight(keystore_path=ks, with_github=False)
    assert result["status"] == "success"


def test_no_auth_skips_logto_and_keystore_checks(monkeypatch, tmp_path):
    _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("LOGTO_M2M_APP_ID", raising=False)
    monkeypatch.delenv("LOGTO_ENDPOINT", raising=False)
    # No keystore at all, but with_logto=False
    result = preflight.run_preflight(
        keystore_path=tmp_path / "nope.json", with_logto=False
    )
    assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Envelope fields
# ---------------------------------------------------------------------------


def test_failure_result_carries_exit_code_and_message(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    result = preflight.run_preflight(keystore_path=ks)
    assert isinstance(result.get("exit_code"), int)
    assert result.get("error_message")


# ---------------------------------------------------------------------------
# Deploy target: Render (U4 / R5 / KTD2)
# ---------------------------------------------------------------------------


_RENDER_WS_OK = {
    "status": "success",
    "workspace_id": "tea-dbgpa4on74is73d2vpk0",
    "workspace_name": "Example Workspace",
}


def _render_good(monkeypatch, calls=None):
    """Make the Render wrapper calls succeed and record which ones ran."""
    calls = calls if calls is not None else []

    def whoami(**k):
        calls.append("whoami")
        return {"status": "success", "email": "owner@example.com"}

    def workspace_current(**k):
        calls.append("workspace_current")
        return dict(_RENDER_WS_OK)

    monkeypatch.setattr(preflight.render, "whoami", whoami)
    monkeypatch.setattr(preflight.render, "workspace_current", workspace_current)
    return calls


def test_render_target_passes_and_returns_workspace(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    calls = _render_good(monkeypatch)
    result = preflight.run_preflight(keystore_path=ks, deploy_target="render")
    assert result["status"] == "success"
    assert result["render_workspace_id"] == "tea-dbgpa4on74is73d2vpk0"
    assert result["render_workspace_name"] == "Example Workspace"
    assert calls == ["whoami", "workspace_current"]


def test_render_target_skips_railway_checks(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    _render_good(monkeypatch)
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    seen = []

    def spy_run(argv, *a, **k):
        seen.append(argv)
        return _ok_proc(0)

    monkeypatch.setattr(preflight.subprocess, "run", spy_run)
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: None if name == "railway" else f"/usr/bin/{name}",
    )
    result = preflight.run_preflight(keystore_path=ks, deploy_target="render")
    assert result["status"] == "success", result
    assert not any(argv and argv[0] == "railway" for argv in seen)


def test_render_target_missing_binary_fails_before_any_call(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    calls = _render_good(monkeypatch)
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: None if name == "render" else f"/usr/bin/{name}",
    )
    result = preflight.run_preflight(keystore_path=ks, deploy_target="render")
    assert result["status"] == "failed"
    assert result["error_code"] == "RENDER_CLI_MISSING"
    assert result["exit_code"] == preflight.EXIT_MISCONFIG
    assert calls == []


def test_render_target_logged_out_fails_with_auth_code(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    calls = _render_good(monkeypatch)
    monkeypatch.setattr(
        preflight.render,
        "whoami",
        lambda **k: {
            "status": "failed",
            "error_code": "RENDER_AUTH_FAILED",
            "exit_code": 1,
            "error_message": "failed to create client: run `render login` to authenticate",
            "retryable": False,
        },
    )
    result = preflight.run_preflight(keystore_path=ks, deploy_target="render")
    assert result["status"] == "failed"
    assert result["checkpoint"] == "preflight"
    assert result["error_code"] == "RENDER_AUTH_FAILED"
    assert result["exit_code"] == preflight.EXIT_AUTH
    assert "render login" in result["error_message"]
    assert "workspace_current" not in calls


def test_render_target_workspace_failure_fails(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    _render_good(monkeypatch)
    monkeypatch.setattr(
        preflight.render,
        "workspace_current",
        lambda **k: {
            "status": "failed",
            "error_code": "RENDER_CLI_ERROR",
            "exit_code": 0,
            "error_message": "render workspace current returned no workspace id",
            "retryable": False,
        },
    )
    result = preflight.run_preflight(keystore_path=ks, deploy_target="render")
    assert result["status"] == "failed"
    assert result["error_code"] == "RENDER_CLI_ERROR"
    assert result["exit_code"] == preflight.EXIT_MISCONFIG


def test_render_target_without_deploy_runs_no_render_or_railway_checks(
    monkeypatch, tmp_path
):
    ks = _all_good(monkeypatch, tmp_path)
    calls = _render_good(monkeypatch)
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: None if name in ("render", "railway") else f"/usr/bin/{name}",
    )
    result = preflight.run_preflight(
        keystore_path=ks, deploy_target="render", with_deploy=False
    )
    assert result["status"] == "success", result
    assert calls == []
    assert "render_workspace_id" not in result


def test_railway_target_makes_no_render_call(monkeypatch, tmp_path):
    ks = _all_good(monkeypatch, tmp_path)
    calls = _render_good(monkeypatch)
    which_seen = []

    def which(name):
        which_seen.append(name)
        return f"/usr/bin/{name}"

    monkeypatch.setattr(preflight.shutil, "which", which)
    result = preflight.run_preflight(keystore_path=ks)
    assert result["status"] == "success"
    assert calls == []
    assert "render" not in which_seen
    assert "railway" in which_seen
    assert "render_workspace_id" not in result
