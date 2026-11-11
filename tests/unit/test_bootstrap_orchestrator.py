"""Unit tests for :mod:`agentic_core.bootstrap.orchestrator`.

The orchestrator wires keystore → logto → github → railway → template →
state together. These tests mock all six modules and assert:

- keystore.decrypt is called exactly once per orchestrate_create_app run
  (PERF-003: never re-decrypt mid-flight).
- Slug validation per spec §R5 exits 1 BEFORE any external call.
- §R10 variant×invariant matrix correctly routes to the four
  ``provision_app`` booleans (full / lightweight-api / static-dashboard
  with-auth / static-dashboard no-auth).
- §R14 idempotency + resume: a partial failure leaves the state file with
  the right checkpoints; ``resume=True`` skips already-done steps.
- Failure envelope shape matches §R3.
- Env hygiene: Git commands receive an environment with
  ``AGENTIC_CORE_ADMIN_PASSPHRASE`` stripped.
- ``dry_run=True`` short-circuits before any external call.
- ``scaffolded_by`` is plumbed into the state file.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path
from unittest import mock

import pytest

from agentic_core.bootstrap import orchestrator
from agentic_core.bootstrap.state import BootstrapState


@pytest.fixture(autouse=True)
def _logto_platform_domain(monkeypatch):
    """Logto runs need a domain (there is no built-in one). The fixtures
    below record resources under example.test, so use that domain."""
    monkeypatch.setenv("PLATFORM_DOMAIN", "example.test")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_all(monkeypatch, tmp_path, *, github_result=None, railway_result=None,
               railway_service_result=None,
               railway_env_result=None, railway_deploy_result=None,
               railway_wait_result=None,
               github_secret_result=None, logto_result=None, raise_in=None,
               preflight_result=None, railway_linked_result=None,
               railway_has_db_result=None, railway_add_db_result=None,
               railway_prod_domain_result=None, logto_redirect_result=None,
               verify_result=None, render_validate_result=None):
    """Patch every external dependency the orchestrator touches.

    Returns a dict of MagicMock objects keyed by short module name so tests
    can introspect calls/args/counts.

    ``preflight_result`` overrides the stubbed ``run_preflight`` return: by
    default preflight PASSES (so existing tests don't need real creds); pass
    a ``{"status": "failed", ...}`` dict to exercise the preflight-abort path.
    """
    # keystore
    decrypt_mock = mock.MagicMock(
        return_value=bytearray(b"admin-secret-bytes")
    )
    load_keystore_mock = mock.MagicMock(return_value={"crypto": {}, "version": 3})
    read_passphrase_mock = mock.MagicMock(return_value="passphrase")
    resolve_path_mock = mock.MagicMock(
        return_value=tmp_path / "admin.keystore.json"
    )
    monkeypatch.setattr(orchestrator.keystore, "decrypt_credential", decrypt_mock)
    monkeypatch.setattr(orchestrator.keystore, "load_keystore", load_keystore_mock)
    monkeypatch.setattr(orchestrator.keystore, "read_passphrase", read_passphrase_mock)
    monkeypatch.setattr(orchestrator.keystore, "resolve_keystore_path", resolve_path_mock)

    # logto.provision_app
    if logto_result is None:
        logto_result = {
            "spa_client_id": "spa-abc",
            "m2m_app_id": "m2m-abc",
            "m2m_app_secret": "secret-abc",
            "api_resource": "https://foo.example.test/api",
            "user_sub": "app_foo",
            "endpoint": "https://logto.test",
            "scopes": ["read", "write"],
        }
    if raise_in == "logto":
        logto_mock = mock.MagicMock(side_effect=RuntimeError("logto blew up"))
    else:
        logto_mock = mock.MagicMock(return_value=logto_result)
    monkeypatch.setattr(orchestrator.logto, "provision_app", logto_mock)

    # github
    if github_result is None:
        github_result = {
            "status": "success",
            "repo": "example-owner/foo",
            "url": "https://github.com/example-owner/foo",
        }
    if raise_in == "github":
        # github wrapper *returns* a failure dict; raising simulates worse path
        github_mock = mock.MagicMock(
            return_value={
                "status": "failed",
                "error_code": "GH_AUTH_FAILED",
                "exit_code": 1,
                "error_message": "not logged in",
            }
        )
    else:
        github_mock = mock.MagicMock(return_value=github_result)
    monkeypatch.setattr(orchestrator.github, "create_github_repo", github_mock)
    # Never call the real gh in unit tests; pretend gh is logged in.
    monkeypatch.setattr(
        orchestrator.github, "logged_in_login", lambda timeout=15: "example-owner"
    )

    if github_secret_result is None:
        github_secret_result = {
            "status": "success", "secret_name": "X", "repo": "example-owner/foo"
        }
    github_secret_mock = mock.MagicMock(return_value=github_secret_result)
    monkeypatch.setattr(orchestrator.github, "set_repo_secret", github_secret_mock)
    github_push_mock = mock.MagicMock(return_value={"status": "success"})
    monkeypatch.setattr(
        orchestrator.github, "initialize_and_push_repo", github_push_mock
    )

    # railway
    if railway_result is None:
        railway_result = {
            "status": "success",
            "name": "foo",
            "workspace": "agent-sandbox",
            "project_id": "proj-1",
        }
    if raise_in == "railway":
        railway_mock = mock.MagicMock(
            return_value={
                "status": "failed",
                "error_code": "RAILWAY_AUTH_FAILED",
                "exit_code": 1,
                "error_message": "railway login required",
            }
        )
    else:
        railway_mock = mock.MagicMock(return_value=railway_result)
    monkeypatch.setattr(orchestrator.railway, "create_railway_project", railway_mock)

    # railway.create_railway_service — creates the service inside the freshly
    # init'd project so subsequent set_env_var / deploy have a target. Default
    # success so full-pipeline tests reach env/deploy.
    if railway_service_result is None:
        railway_service_result = {
            "status": "success",
            "service": "foo",
            "service_id": "svc-abc",
        }
    railway_service_mock = mock.MagicMock(return_value=railway_service_result)
    monkeypatch.setattr(
        orchestrator.railway, "create_railway_service", railway_service_mock
    )

    # railway.get_linked_project — the check() helper. Default: NOT linked,
    # so the orchestrator falls through to create_railway_project (init).
    # Tests exercising the adopt-on-resume path pass
    # ``railway_linked_result={"status": "success", "linked": True, ...}``.
    if railway_linked_result is None:
        railway_linked_result = {"status": "success", "linked": False}
    railway_linked_mock = mock.MagicMock(return_value=railway_linked_result)
    monkeypatch.setattr(
        orchestrator.railway, "get_linked_project", railway_linked_mock
    )

    # railway.has_database — the check() helper for the Postgres step. Default:
    # NO existing DB, so the orchestrator falls through to add_database. Tests
    # exercising resume-with-DB pass
    # ``railway_has_db_result={"status": "success", "has_database": True}``.
    if railway_has_db_result is None:
        railway_has_db_result = {"status": "success", "has_database": False}
    railway_has_db_mock = mock.MagicMock(return_value=railway_has_db_result)
    monkeypatch.setattr(orchestrator.railway, "has_database", railway_has_db_mock)

    # railway.add_database — provisions Postgres. Default success so existing
    # full-pipeline tests don't hit real subprocess.
    if railway_add_db_result is None:
        railway_add_db_result = {"status": "success"}
    railway_add_db_mock = mock.MagicMock(return_value=railway_add_db_result)
    monkeypatch.setattr(orchestrator.railway, "add_database", railway_add_db_mock)

    # railway.get_prod_domain — STEP_WIRING capture of the assigned prod domain
    # via `railway domain --json`. Default success so full-pipeline tests reach
    # the CORS + redirect-URI wiring.
    if railway_prod_domain_result is None:
        railway_prod_domain_result = {
            "status": "success",
            "domain": "foo-production.up.railway.app",
        }
    railway_prod_domain_mock = mock.MagicMock(return_value=railway_prod_domain_result)
    monkeypatch.setattr(
        orchestrator.railway, "get_prod_domain", railway_prod_domain_mock
    )

    # logto.update_app_redirect_uris — STEP_WIRING merge-PATCH of the SPA app's
    # prod redirect URI. Default: reports the merged post-state including the
    # prod /callback so the orchestrator's structural assert passes.
    if logto_redirect_result is None:
        logto_redirect_result = {
            "redirect_uris": [
                "http://localhost:5173/callback",
                "https://foo-production.up.railway.app/callback",
            ],
            "patched": True,
        }
    logto_redirect_mock = mock.MagicMock(return_value=logto_redirect_result)
    monkeypatch.setattr(
        orchestrator.logto, "update_app_redirect_uris", logto_redirect_mock
    )

    if railway_env_result is None:
        railway_env_result = {"status": "success", "name": "X"}
    railway_env_mock = mock.MagicMock(return_value=railway_env_result)
    monkeypatch.setattr(orchestrator.railway, "set_env_var", railway_env_mock)

    # railway.link_project — links the app dir to project+service before deploy.
    railway_link_mock = mock.MagicMock(
        return_value={"status": "success", "project_id": "proj-1", "service": "foo"}
    )
    monkeypatch.setattr(orchestrator.railway, "link_project", railway_link_mock)

    if railway_deploy_result is None:
        railway_deploy_result = {"status": "success"}
    railway_deploy_mock = mock.MagicMock(return_value=railway_deploy_result)
    monkeypatch.setattr(orchestrator.railway, "deploy", railway_deploy_mock)

    if railway_wait_result is None:
        railway_wait_result = {"status": "success", "deployment_status": "SUCCESS"}
    railway_wait_mock = mock.MagicMock(return_value=railway_wait_result)
    monkeypatch.setattr(
        orchestrator.railway, "wait_for_deployment", railway_wait_mock, raising=False
    )

    # verify.verify_deployment — STEP_VERIFY final gate (health + auth). Default
    # SUCCESS so existing full-pipeline tests reach the success/state-delete
    # block instead of hitting the real network. Tests exercising the
    # transient/structural routing pass ``verify_result={"status": ...}``.
    if verify_result is None:
        verify_result = {"status": "success", "auth_checked": True}
    verify_mock = mock.MagicMock(return_value=verify_result)
    monkeypatch.setattr(orchestrator.verify, "verify_deployment", verify_mock)

    # template
    render_template_mock = mock.MagicMock(return_value=None)
    monkeypatch.setattr(orchestrator.template, "render_template", render_template_mock)

    # preflight — stubbed to PASS by default so existing tests don't need
    # real gh/railway logins, env vars, or an on-disk keystore. Tests that
    # exercise the abort path pass ``preflight_result={"status": "failed", ...}``.
    if preflight_result is None:
        preflight_result = {"status": "passed", "checkpoint": "preflight"}
    preflight_mock = mock.MagicMock(return_value=preflight_result)
    monkeypatch.setattr(orchestrator.preflight, "run_preflight", preflight_mock)

    # render.validate_blueprint — the Render-target validate step. Patched on
    # the render module itself so it is stubbed however the orchestrator
    # reaches it. Default: the Blueprint is valid.
    from agentic_core.bootstrap import render as render_mod

    if render_validate_result is None:
        render_validate_result = {"status": "success", "valid": True, "plan": {}}
    render_validate_mock = mock.MagicMock(return_value=render_validate_result)
    monkeypatch.setattr(render_mod, "validate_blueprint", render_validate_mock)

    # state — write_state writes a file; we let it actually write to tmp
    return {
        "render_validate": render_validate_mock,
        "preflight": preflight_mock,
        "decrypt": decrypt_mock,
        "load_keystore": load_keystore_mock,
        "read_passphrase": read_passphrase_mock,
        "resolve_path": resolve_path_mock,
        "logto": logto_mock,
        "github": github_mock,
        "github_secret": github_secret_mock,
        "github_push": github_push_mock,
        "railway": railway_mock,
        "railway_service": railway_service_mock,
        "railway_linked": railway_linked_mock,
        "railway_has_db": railway_has_db_mock,
        "railway_add_db": railway_add_db_mock,
        "railway_env": railway_env_mock,
        "railway_link": railway_link_mock,
        "railway_deploy": railway_deploy_mock,
        "railway_wait": railway_wait_mock,
        "railway_prod_domain": railway_prod_domain_mock,
        "logto_redirect": logto_redirect_mock,
        "verify": verify_mock,
        "render_template": render_template_mock,
    }


# ---------------------------------------------------------------------------
# Slug validation (§R5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_slug",
    [
        "Foo",          # uppercase
        "-bar",         # leading hyphen
        "foo bar",      # space
        "a",            # too short (regex requires >=3 chars total)
        "foo--",        # trailing hyphen
        "foo_bar",      # underscore not in class
        "a" * 60,       # too long (>50)
        "",             # empty
        "FOO",
        "foo.bar",
    ],
)
def test_invalid_slug_exits_one_before_external_calls(bad_slug, monkeypatch, tmp_path):
    """§R5: invalid slug → SystemExit(1) BEFORE any external module fires."""
    mocks = _patch_all(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug=bad_slug,
            description="x",
            keystore_path=tmp_path / "admin.keystore.json",
        )
    assert exc_info.value.code == 1
    # No external call fired:
    mocks["decrypt"].assert_not_called()
    mocks["load_keystore"].assert_not_called()
    mocks["logto"].assert_not_called()
    mocks["github"].assert_not_called()
    mocks["railway"].assert_not_called()
    mocks["render_template"].assert_not_called()


@pytest.mark.parametrize(
    "good_slug",
    [
        "foo",
        "foo-bar",
        "foo-bar-baz",
        "yc-companies",
        "abc123",
        "a1b",
        "habit-tracker",
    ],
)
def test_valid_slug_proceeds_past_validation(good_slug, monkeypatch, tmp_path):
    """Sanity check: good slugs reach the keystore decrypt step."""
    mocks = _patch_all(monkeypatch, tmp_path)
    output_dir = tmp_path / "out"
    result = orchestrator.orchestrate_create_app(
        slug=good_slug,
        description="x",
        keystore_path=tmp_path / "admin.keystore.json",
        passphrase_fd=None,
        with_railway=False,
        with_github=False,
        scaffolded_by="manual",
        output_dir=output_dir,
        railway_workspace=None,
    )
    assert result["status"] == "success"
    assert mocks["decrypt"].call_count == 1


# ---------------------------------------------------------------------------
# PERF-003: keystore decrypted exactly once
# ---------------------------------------------------------------------------


def test_decrypt_called_exactly_once(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        keystore_path=tmp_path / "admin.keystore.json",
        passphrase_fd=None,
        with_railway=True,
        with_github=True,
        with_logto=True,
        scaffolded_by="agent",
        output_dir=tmp_path / "out",
        railway_workspace="agent-sandbox",
    )
    assert mocks["decrypt"].call_count == 1


def test_decrypt_not_called_when_with_logto_false(monkeypatch, tmp_path):
    """If --no-auth (with_logto=False), decrypt MUST NOT be called."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="static-dashboard",
        with_auth=False,
        with_logto=False,
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        scaffolded_by="manual",
        output_dir=tmp_path / "out",
    )
    mocks["decrypt"].assert_not_called()
    mocks["logto"].assert_not_called()


# ---------------------------------------------------------------------------
# §R10 variant × invariant matrix routing to provision_app
# ---------------------------------------------------------------------------


def test_full_variant_passes_all_four_booleans_true(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="full",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    mocks["logto"].assert_called_once()
    kwargs = mocks["logto"].call_args.kwargs
    assert kwargs.get("with_spa") is True
    assert kwargs.get("with_m2m") is True
    assert kwargs.get("with_api_resource") is True
    assert kwargs.get("with_user") is True


def test_lightweight_api_variant_skips_spa(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="lightweight-api",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    mocks["logto"].assert_called_once()
    kwargs = mocks["logto"].call_args.kwargs
    assert kwargs.get("with_spa") is False
    assert kwargs.get("with_m2m") is True
    assert kwargs.get("with_api_resource") is True
    assert kwargs.get("with_user") is True


def test_static_dashboard_with_auth_only_spa_and_user(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="static-dashboard",
        with_auth=True,
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    mocks["logto"].assert_called_once()
    kwargs = mocks["logto"].call_args.kwargs
    assert kwargs.get("with_spa") is True
    assert kwargs.get("with_m2m") is False
    assert kwargs.get("with_api_resource") is False
    assert kwargs.get("with_user") is True


def test_static_dashboard_no_auth_skips_logto_entirely(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="static-dashboard",
        with_auth=False,
        with_logto=False,
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    mocks["logto"].assert_not_called()
    mocks["decrypt"].assert_not_called()


# ---------------------------------------------------------------------------
# Failure envelope & exit code
# ---------------------------------------------------------------------------


def test_logto_failure_returns_failure_envelope_and_state_records_partial(
    monkeypatch, tmp_path
):
    _patch_all(monkeypatch, tmp_path, raise_in="logto")
    output_dir = tmp_path / "out"
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=True,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=output_dir,
            state_path=state_path,
            scaffolded_by="manual",
        )
    assert exc_info.value.code != 0
    # The state file should record logto.done = False (failure during step)
    # but the keystore.done should be True (decrypt succeeded).
    assert state_path.exists()
    raw = state_path.read_text()
    assert "keystore" in raw
    # logto checkpoint should NOT be marked done (Pydantic emits compact JSON)
    assert '"logto":true' not in raw
    assert '"logto": true' not in raw


def test_github_failure_returns_failure_envelope(monkeypatch, tmp_path):
    """A wrapper returning a {status: failed, ...} envelope is treated as fatal."""
    _patch_all(monkeypatch, tmp_path, raise_in="github")
    output_dir = tmp_path / "out"
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=True,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=output_dir,
            state_path=state_path,
            scaffolded_by="manual",
        )
    assert exc_info.value.code != 0
    # State must reflect: logto done, github not done (Pydantic compact JSON)
    assert state_path.exists()
    raw = state_path.read_text()
    assert '"logto":true' in raw or '"logto": true' in raw
    assert '"github":true' not in raw
    assert '"github": true' not in raw


# ---------------------------------------------------------------------------
# Resume: --resume skips already-done steps
# ---------------------------------------------------------------------------


def test_resume_skips_already_done_steps(monkeypatch, tmp_path):
    """After the credentialed steps (keystore/logto/github/secrets/railway/
    railway_env) succeeded but a later, non-secret step (Postgres) was still
    pending, --resume must NOT re-call provision_app / create_github_repo /
    create_railway_project — it skips straight to the remaining steps.

    NOTE: resuming past a checkpointed STEP_LOGTO is only safe once the
    secret-dependent steps (github_secrets, railway_env) are already done — the
    M2M app secret only lives in memory during the original STEP_LOGTO run (see
    Fix 1 / LOGTO_SECRET_UNRECOVERABLE), so this seed marks them complete."""
    mocks = _patch_all(monkeypatch, tmp_path)
    output_dir = tmp_path / "out"
    state_path = tmp_path / ".bootstrap-state.json"
    # Pre-write a state file where every credentialed step completed; only the
    # non-secret Postgres/wiring/deploy/verify steps remain.
    pre_state = BootstrapState(
        app_slug="foo",
        variant="full",
        scaffolded_by="manual",
        created_at=datetime.now(tz=UTC),
        checkpoints={
            "keystore": True,
            "logto": True,
            "github": True,
            "github_secrets": True,
            "railway": True,
            "railway_env": True,
        },
        resource_ids={
            "logto_m2m_app_id": "m2m-abc",
            "logto_spa_client_id": "spa-abc",
            "logto_api_resource": "https://foo.example.test/api",
            "logto_user_sub": "app_foo",
            "railway_project_id": _RW_UUID,
        },
        last_completed_step="railway_env",
    )
    from agentic_core.bootstrap import state as state_mod
    state_mod.write_state(state_path, pre_state)

    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=output_dir,
        state_path=state_path,
        scaffolded_by="manual",
        resume=True,
    )
    assert result["status"] == "success"
    # Credentialed create steps were already done → skipped, not re-run.
    mocks["logto"].assert_not_called()
    mocks["decrypt"].assert_not_called()
    mocks["github"].assert_not_called()
    mocks["railway"].assert_not_called()
    # The still-pending non-secret steps run to completion.
    mocks["railway_add_db"].assert_called_once()
    mocks["verify"].assert_called_once()


# ---------------------------------------------------------------------------
# Idempotency: orchestrator forwards Logto's "find or create" behavior
# ---------------------------------------------------------------------------


def test_idempotent_orchestrator_does_not_dedupe_logic(monkeypatch, tmp_path):
    """The orchestrator delegates idempotency to provision_app (which already
    has find-then-create). Calling orchestrate_create_app twice with the same
    slug should call provision_app twice (Logto's per-call find_application
    handles the duplicate-avoidance)."""
    mocks = _patch_all(monkeypatch, tmp_path)
    output_dir = tmp_path / "out"
    for _ in range(2):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            variant="full",
            with_railway=False,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=output_dir / f"run-{mocks['logto'].call_count}",
            scaffolded_by="manual",
        )
    # provision_app was called twice (orchestrator forwards each time)
    assert mocks["logto"].call_count == 2
    # provision_app's internal find_application is what makes it idempotent —
    # not the orchestrator. We verify the orchestrator just forwards.


# ---------------------------------------------------------------------------
# Git subprocess env hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("env_var_present", [True, False])
def test_subprocess_env_excludes_admin_passphrase(monkeypatch, tmp_path, env_var_present):
    """The Git wrapper receives a sanitized environment."""
    mocks = _patch_all(monkeypatch, tmp_path)
    if env_var_present:
        monkeypatch.setenv("AGENTIC_CORE_ADMIN_PASSPHRASE", "should-not-leak")

    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_logto=False,
        with_railway=False,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    env = mocks["github_push"].call_args.kwargs["env"]
    assert "AGENTIC_CORE_ADMIN_PASSPHRASE" not in env


def test_strip_env_helper_removes_admin_passphrase(monkeypatch):
    """Unit test the helper directly."""
    monkeypatch.setenv("AGENTIC_CORE_ADMIN_PASSPHRASE", "leak")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = orchestrator._sanitized_env()
    assert "AGENTIC_CORE_ADMIN_PASSPHRASE" not in env
    assert env.get("PATH") == "/usr/bin"


def test_git_push_failure_stops_pipeline_and_remains_retryable(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    mocks["github_push"].return_value = {
        "status": "failed",
        "error_code": "GH_GIT_ERROR",
        "exit_code": 128,
        "error_message": "connection refused",
    }

    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_logto=False,
        with_railway=False,
        with_github=True,
        output_dir=tmp_path / "out",
        state_path=tmp_path / "state.json",
        raise_on_failure=False,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "GH_GIT_ERROR"
    assert result["checkpoint"] == "git_commit"
    assert "git_commit" not in result["completed_steps"]


# ---------------------------------------------------------------------------
# dry_run short-circuits
# ---------------------------------------------------------------------------


def test_dry_run_skips_all_external_calls(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="full",
        dry_run=True,
        with_railway=True,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="agent",
    )
    mocks["decrypt"].assert_not_called()
    mocks["logto"].assert_not_called()
    mocks["github"].assert_not_called()
    mocks["railway"].assert_not_called()
    mocks["render_template"].assert_not_called()
    assert result["status"] == "success"
    # Even a dry run carries the slug/variant + the workspace and
    # scaffolded_by so an agent caller can validate the plan it's about to commit.
    assert result["app_slug"] == "foo"
    assert result["variant"] == "full"
    assert result.get("dry_run") is True


# ---------------------------------------------------------------------------
# scaffolded_by passthrough
# ---------------------------------------------------------------------------


def test_scaffolded_by_recorded_in_state(monkeypatch, tmp_path):
    """The orchestrator must persist scaffolded_by into the state file."""
    _patch_all(monkeypatch, tmp_path)
    state_path = tmp_path / ".bootstrap-state.json"
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        scaffolded_by="agent",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
    )
    # state file is deleted on success → assert by reading the result envelope
    # which mirrors the state we just wrote
    # If the state file was deleted (success), check the returned envelope:
    # The result envelope must include scaffolded_by.
    # Re-run with a forced failure to inspect the persisted state:
    state_path2 = tmp_path / "out2-state.json"
    _patch_all(monkeypatch, tmp_path, raise_in="logto")
    with pytest.raises(SystemExit):
        orchestrator.orchestrate_create_app(
            slug="bar",
            description="x",
            scaffolded_by="agent",
            with_railway=False,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out2",
            state_path=state_path2,
        )
    raw = state_path2.read_text()
    assert '"scaffolded_by":"agent"' in raw or '"scaffolded_by": "agent"' in raw


# ---------------------------------------------------------------------------
# Success envelope shape (R3)
# ---------------------------------------------------------------------------


def test_success_envelope_has_required_fields(monkeypatch, tmp_path):
    _patch_all(monkeypatch, tmp_path)
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="A test app",
        variant="full",
        with_railway=True,
        with_github=True,
        railway_workspace="agent-sandbox",
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="agent",
    )
    assert result["status"] == "success"
    assert result["schema_version"] == 1
    assert result["app_slug"] == "foo"
    assert result["variant"] == "full"
    assert result["scaffolded_by"] == "agent"
    assert "logto" in result
    assert "github" in result
    assert "railway" in result
    assert result["railway"]["workspace"] == "agent-sandbox"
    # completed_steps records what ran
    assert isinstance(result.get("completed_steps", []), list)
    assert "logto" in result["completed_steps"]
    assert "github" in result["completed_steps"]
    assert "railway" in result["completed_steps"]


def test_failure_envelope_has_required_fields(monkeypatch, tmp_path):
    """§R3 failure envelope shape."""
    _patch_all(monkeypatch, tmp_path, raise_in="logto")
    state_path = tmp_path / ".bootstrap-state.json"
    captured = {}

    def capture_exit(code):
        captured["code"] = code
        raise SystemExit(code)

    # We rely on the orchestrator raising SystemExit with a non-zero code on
    # terminal failure, and on emitting a failure envelope returned-or-printed
    # before exiting. Smallest contract: the orchestrator must surface the
    # failure shape via the SystemExit exception's args[1] OR via a callback.
    # We check the state file (persistence) and the SystemExit code.
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=False,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    # Exit code is non-zero
    assert exc_info.value.code != 0
    # State file written and includes checkpoint info
    assert state_path.exists()


# ---------------------------------------------------------------------------
# Render template wires variant + opt-out flags
# ---------------------------------------------------------------------------


def test_render_template_receives_variant(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="lightweight-api",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    mocks["render_template"].assert_called_once()
    call = mocks["render_template"].call_args
    # variant should be plumbed
    assert call.kwargs.get("variant") == "lightweight-api" or (
        len(call.args) >= 4 and call.args[3] == "lightweight-api"
    )


# ---------------------------------------------------------------------------
# R8: retryable classification on the failure envelope
# ---------------------------------------------------------------------------


def test_fail_default_is_retryable():
    """``_fail`` defaults ``retryable`` to True (backward compatible)."""
    envelope = orchestrator._fail(
        app_slug="foo",
        checkpoint="logto",
        exit_code=3,
        error_code="LOGTO_API_FAILED",
        error_message="boom",
        completed_steps=[],
        state_path=None,
        state_obj=None,
        raise_on_failure=False,
    )
    assert envelope["retryable"] is True


def test_fail_retryable_false_marks_envelope_non_retryable():
    """A structural failure calls ``_fail(retryable=False)`` → envelope reflects it."""
    envelope = orchestrator._fail(
        app_slug="foo",
        checkpoint="template",
        exit_code=3,
        error_code="TEMPLATE_RENDER_FAILED",
        error_message="broken app",
        completed_steps=[],
        state_path=None,
        state_obj=None,
        raise_on_failure=False,
        retryable=False,
    )
    assert envelope["retryable"] is False


def test_existing_failure_path_envelope_is_retryable_and_schema_valid(monkeypatch, tmp_path):
    """Existing failure paths still serialize a valid FailureEnvelope; retryable defaults True."""
    from agentic_core.output_schema import FailureEnvelope

    _patch_all(monkeypatch, tmp_path, raise_in="logto")
    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    # Backward-compat: envelope validates against the schema and is retryable.
    model = FailureEnvelope.model_validate(envelope)
    assert model.retryable is True


# ---------------------------------------------------------------------------
# U1: preflight aborts BEFORE any external call (§R6 / SC4)
# ---------------------------------------------------------------------------


def test_preflight_failure_aborts_before_any_external_call(monkeypatch, tmp_path):
    """A failing preflight returns a non-retryable envelope and NO provider
    method (Logto / GitHub / Railway / keystore decrypt) is ever called."""
    from agentic_core.output_schema import FailureEnvelope

    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        preflight_result={
            "status": "failed",
            "checkpoint": "preflight",
            "error_code": "PREFLIGHT_RAILWAY_MISSING",
            "exit_code": 5,
            "error_message": "`railway` binary not found on PATH.",
        },
    )
    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    # Preflight ran exactly once.
    mocks["preflight"].assert_called_once()
    # NOTHING downstream fired.
    mocks["decrypt"].assert_not_called()
    mocks["load_keystore"].assert_not_called()
    mocks["logto"].assert_not_called()
    mocks["github"].assert_not_called()
    mocks["github_secret"].assert_not_called()
    mocks["railway"].assert_not_called()
    mocks["railway_env"].assert_not_called()
    mocks["railway_deploy"].assert_not_called()
    mocks["render_template"].assert_not_called()
    # Envelope is schema-valid and non-retryable (structural: nothing created).
    model = FailureEnvelope.model_validate(envelope)
    assert model.error_code == "PREFLIGHT_RAILWAY_MISSING"
    assert model.exit_code == 5
    assert model.retryable is False


def test_preflight_failure_raises_systemexit_by_default(monkeypatch, tmp_path):
    """With raise_on_failure=True (default) a preflight failure raises
    SystemExit with the preflight exit code + the envelope stashed."""
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        preflight_result={
            "status": "failed",
            "checkpoint": "preflight",
            "error_code": "PREFLIGHT_GH_AUTH_FAILED",
            "exit_code": 2,
            "error_message": "`gh auth status` failed.",
        },
    )
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            scaffolded_by="manual",
        )
    assert exc_info.value.code == 2
    envelope = getattr(exc_info.value, "envelope", None)
    assert envelope is not None
    assert envelope["error_code"] == "PREFLIGHT_GH_AUTH_FAILED"
    assert envelope["retryable"] is False
    mocks["logto"].assert_not_called()


def test_preflight_skipped_on_dry_run(monkeypatch, tmp_path):
    """--dry-run is a plan preview — it must not require live creds, so
    preflight is not consulted."""
    mocks = _patch_all(monkeypatch, tmp_path)
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        dry_run=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    assert result["status"] == "success"
    mocks["preflight"].assert_not_called()


def test_preflight_receives_provider_flags(monkeypatch, tmp_path):
    """The orchestrator forwards the with_railway/with_github/with_logto
    opt-outs so preflight doesn't demand creds for skipped providers."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="static-dashboard",
        with_auth=False,
        with_logto=False,
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    kwargs = mocks["preflight"].call_args.kwargs
    assert kwargs.get("with_railway") is False
    assert kwargs.get("with_github") is False
    assert kwargs.get("with_logto") is False


def test_structural_failure_path_marks_non_retryable(monkeypatch, tmp_path):
    """A structural failure at a real call site (invalid slug) → retryable False."""
    from agentic_core.output_schema import FailureEnvelope

    _patch_all(monkeypatch, tmp_path)
    envelope = orchestrator.orchestrate_create_app(
        slug="Bad Slug",  # fails §R5 → structural, non-retryable
        description="x",
        with_railway=False,
        with_github=False,
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    model = FailureEnvelope.model_validate(envelope)
    assert model.error_code == "INVALID_SLUG"
    assert model.retryable is False


# ---------------------------------------------------------------------------
# U3: Railway --json project-ID capture + init idempotency (adopt on resume)
# ---------------------------------------------------------------------------


_RW_UUID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


def test_railway_project_id_from_init_persisted_to_state(monkeypatch, tmp_path):
    """R2: the project id parsed from `railway init --json` is persisted into
    ``resource_ids['railway_project_id']``. We force a downstream (deploy)
    failure so the state file survives for inspection."""
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        railway_result={
            "status": "success",
            "name": "foo",
            "workspace": None,
            "project_id": _RW_UUID,
        },
        railway_deploy_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "deploy boom",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    # init (create_railway_project) was called; the check() saw no linked project.
    mocks["railway_linked"].assert_called_once()
    mocks["railway"].assert_called_once()
    raw = state_path.read_text()
    assert "railway_project_id" in raw
    assert _RW_UUID in raw


def test_railway_adopts_existing_linked_project_and_skips_init(monkeypatch, tmp_path):
    """R3 / SC2: when a project is already linked, `railway init` is NOT
    re-run — the orchestrator adopts the existing project and persists its id."""
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        railway_linked_result={
            "status": "success",
            "linked": True,
            "project_id": _RW_UUID,
            "name": "foo",
            "service_id": None,
        },
        railway_deploy_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "deploy boom",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    # check() ran and reported linked → init must be SKIPPED entirely.
    mocks["railway_linked"].assert_called_once()
    mocks["railway"].assert_not_called()
    # Adopted project id is still persisted to state.
    raw = state_path.read_text()
    assert "railway_project_id" in raw
    assert _RW_UUID in raw


def test_railway_linked_project_name_mismatch_fails_non_retryable(monkeypatch, tmp_path):
    """Finding 1 (P0): when the cwd is linked to a DIFFERENT Railway project
    (linked name != slug), the orchestrator must NOT adopt it — it fails loud,
    non-retryable (RAILWAY_PROJECT_MISMATCH), and touches no env/postgres/deploy."""
    from agentic_core.output_schema import FailureEnvelope

    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        railway_linked_result={
            "status": "success",
            "linked": True,
            "project_id": _RW_UUID,
            "name": "some-other-app",
            "service_id": None,
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    model = FailureEnvelope.model_validate(envelope)
    assert model.status == "failed"
    assert model.error_code == "RAILWAY_PROJECT_MISMATCH"
    assert model.retryable is False
    assert model.checkpoint == "railway"
    # init was NOT run, and nothing downstream fired.
    mocks["railway"].assert_not_called()
    mocks["railway_env"].assert_not_called()
    mocks["railway_add_db"].assert_not_called()
    mocks["railway_deploy"].assert_not_called()
    mocks["verify"].assert_not_called()


def test_railway_linked_project_name_none_fails_mismatch(monkeypatch, tmp_path):
    """Finding 1: if the linked project's name can't be determined at all
    (None), it is NOT a confident match → same safe RAILWAY_PROJECT_MISMATCH
    failure rather than adopting an unverifiable project."""
    from agentic_core.output_schema import FailureEnvelope

    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        railway_linked_result={
            "status": "success",
            "linked": True,
            "project_id": _RW_UUID,
            "name": None,
            "service_id": None,
        },
    )
    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    model = FailureEnvelope.model_validate(envelope)
    assert model.status == "failed"
    assert model.error_code == "RAILWAY_PROJECT_MISMATCH"
    assert model.retryable is False
    mocks["railway"].assert_not_called()
    mocks["railway_deploy"].assert_not_called()


def test_railway_project_id_uuid_survives_entropy_gate(monkeypatch, tmp_path):
    """R2: a 36-char hyphenated Railway UUID written to state via the normal
    `write_state` path does not trip the R8 high-entropy gate (hyphens split
    it into sub-32-char runs)."""
    import re

    entropy_re = re.compile(r"[A-Za-z0-9+/=_]{32,}")
    _patch_all(
        monkeypatch,
        tmp_path,
        railway_result={
            "status": "success",
            "name": "foo",
            "workspace": None,
            "project_id": _RW_UUID,
        },
        railway_deploy_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "deploy boom",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    raw = state_path.read_text()
    assert _RW_UUID in raw  # it was written
    assert entropy_re.findall(raw) == []  # but no high-entropy run tripped


# ---------------------------------------------------------------------------
# Railway service creation + service-targeting. `railway init` makes an EMPTY
# project with NO service; the next `railway variables --set` fails with
# "Project has no services". STEP_RAILWAY must create a service after init, and
# env/wiring/deploy must target it via service=slug.
# ---------------------------------------------------------------------------


def test_railway_creates_service_after_init(monkeypatch, tmp_path):
    """Fresh run: after create_railway_project (init), create_railway_service is
    called exactly once with service=slug so the project has a service."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    mocks["railway"].assert_called_once()  # init
    mocks["railway_service"].assert_called_once()
    assert mocks["railway_service"].call_args.kwargs.get("service") == "foo"


def test_railway_creates_service_on_adopt_path(monkeypatch, tmp_path):
    """When an existing linked project is adopted (init skipped), the service is
    STILL ensured (create_railway_service is idempotent) so env/deploy have a
    target."""
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        railway_linked_result={
            "status": "success",
            "linked": True,
            "project_id": _RW_UUID,
            "name": "foo",
            "service_id": None,
        },
    )
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    mocks["railway"].assert_not_called()  # init skipped (adopted)
    mocks["railway_service"].assert_called_once()
    assert mocks["railway_service"].call_args.kwargs.get("service") == "foo"


def test_railway_service_failure_is_retryable(monkeypatch, tmp_path):
    """A create_railway_service failure routes through _fail as retryable
    (transient) and does NOT mark STEP_RAILWAY done."""
    _patch_all(
        monkeypatch,
        tmp_path,
        railway_service_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "service boom",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    envelope = exc_info.value.envelope  # type: ignore[attr-defined]
    assert envelope["checkpoint"] == "railway"
    assert envelope["retryable"] is True
    raw = state_path.read_text()
    assert '"railway":true' not in raw
    assert '"railway": true' not in raw


def test_railway_env_calls_pass_service(monkeypatch, tmp_path):
    """STEP_RAILWAY_ENV must target the app's service via service=slug on every
    set_env_var call (required once the project has app + Postgres services)."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    assert mocks["railway_env"].call_count > 0
    for c in mocks["railway_env"].call_args_list:
        assert c.kwargs.get("service") == "foo"


def test_railway_deploy_targets_project_service_and_app_dir(monkeypatch, tmp_path):
    """STEP_RAILWAY_DEPLOY must deploy the rendered app dir to the project +
    service EXPLICITLY (path + project_id + service), so `railway up` doesn't
    depend on a per-directory link the app dir doesn't have."""
    mocks = _patch_all(monkeypatch, tmp_path)
    output_dir = tmp_path / "out"
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=output_dir,
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    # STEP_RAILWAY_DEPLOY links the app dir first, then deploys from it.
    mocks["railway_link"].assert_called_once()
    link_kwargs = mocks["railway_link"].call_args.kwargs
    assert link_kwargs.get("service") == "foo"
    assert str(link_kwargs.get("cwd")) == str(output_dir / "foo")
    assert link_kwargs.get("project_id")  # from the (mocked) railway init result

    mocks["railway_deploy"].assert_called_once()
    kwargs = mocks["railway_deploy"].call_args.kwargs
    assert kwargs.get("service") == "foo"
    # Deploy runs FROM the linked app dir (cwd), no -p/-e/path.
    assert str(kwargs.get("cwd")) == str(output_dir / "foo")


def test_all_railway_provisioning_targets_app_dir(monkeypatch, tmp_path):
    """Every directory-scoped Railway command must use the per-app directory.

    In particular, ``railway init`` links its cwd. Letting any provisioning
    call inherit the agentic-core process cwd contaminates the bootstrap repo
    and makes the next app adopt or reject the previous app's project.
    """
    mocks = _patch_all(monkeypatch, tmp_path)
    output_dir = tmp_path / "out"
    app_dir = output_dir / "foo"

    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=output_dir,
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )

    single_call_mocks = (
        mocks["railway_linked"],
        mocks["railway"],
        mocks["railway_service"],
        mocks["railway_has_db"],
        mocks["railway_add_db"],
        mocks["railway_prod_domain"],
        mocks["railway_link"],
        mocks["railway_deploy"],
        mocks["railway_wait"],
    )
    for railway_mock in single_call_mocks:
        assert railway_mock.call_args.kwargs.get("cwd") == str(app_dir)

    assert mocks["railway_env"].call_args_list
    for call in mocks["railway_env"].call_args_list:
        assert call.kwargs.get("cwd") == str(app_dir)


def test_multiline_description_is_normalized_before_rendering(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)

    orchestrator.orchestrate_create_app(
        slug="foo",
        description="competitor intelligence, and\n    lead generation",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )

    copier_data = mocks["render_template"].call_args.kwargs["data"]
    assert copier_data["app_description"] == (
        "competitor intelligence, and lead generation"
    )


@pytest.mark.parametrize("description", ["", "   \n\t "])
def test_blank_description_uses_the_template_default(monkeypatch, tmp_path, description):
    """D4: an omitted or blank --description must not render an empty description."""
    mocks = _patch_all(monkeypatch, tmp_path)

    orchestrator.orchestrate_create_app(
        slug="foo",
        description=description,
        with_railway=False,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )

    copier_data = mocks["render_template"].call_args.kwargs["data"]
    assert copier_data["app_description"] == "A mini-app"
    # GitHub gets the same value as the app.
    assert mocks["github"].call_args.kwargs["description"] == "A mini-app"


def test_default_description_matches_the_copier_default():
    """The orchestrator default and copier.yml must not drift apart."""
    import yaml

    copier_yml = Path(__file__).resolve().parents[2] / "templates" / "mini-app" / "copier.yml"
    config = yaml.safe_load(copier_yml.read_text())
    assert config["app_description"]["default"] == orchestrator.DEFAULT_APP_DESCRIPTION


def test_no_mobile_is_forwarded_to_template(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)

    orchestrator.orchestrate_create_app(
        slug="foo",
        variant="full",
        with_mobile=False,
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )

    copier_data = mocks["render_template"].call_args.kwargs["data"]
    assert copier_data["include_mobile"] is False


def test_progress_reports_completed_and_long_running_steps(monkeypatch, tmp_path):
    _patch_all(monkeypatch, tmp_path)
    messages: list[str] = []

    orchestrator.orchestrate_create_app(
        slug="foo",
        with_railway=True,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
        progress=messages.append,
    )

    assert any("Logto" in message for message in messages)
    assert any("GitHub" in message for message in messages)
    assert any("Waiting for Railway" in message for message in messages)
    assert any("verification passed" in message.lower() for message in messages)


def test_failed_railway_build_stops_before_health_verification(monkeypatch, tmp_path):
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        railway_wait_result={
            "status": "failed",
            "error_code": "RAILWAY_DEPLOY_FAILED",
            "error_message": "Railway deployment reached FAILED",
        },
    )

    result = orchestrator.orchestrate_create_app(
        slug="foo",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
        raise_on_failure=False,
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "RAILWAY_DEPLOY_FAILED"
    mocks["verify"].assert_not_called()


def test_wiring_get_prod_domain_passes_service(monkeypatch, tmp_path):
    """STEP_WIRING captures the domain for the app's service via service=slug."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    mocks["railway_prod_domain"].assert_called_once()
    assert mocks["railway_prod_domain"].call_args.kwargs.get("service") == "foo"


def test_railway_service_id_persisted_to_state(monkeypatch, tmp_path):
    """The captured service id is persisted into resource_ids (IDs only). Force a
    downstream deploy failure so the state file survives for inspection."""
    _patch_all(
        monkeypatch,
        tmp_path,
        railway_service_result={
            "status": "success",
            "service": "foo",
            "service_id": "svc-xyz",
        },
        railway_deploy_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "deploy boom",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    raw = state_path.read_text()
    assert "railway_service_id" in raw
    assert "svc-xyz" in raw


# ---------------------------------------------------------------------------
# U4: incremental Logto resource-ID persistence via a persist callback
# ---------------------------------------------------------------------------


def test_orchestrator_passes_persist_callback_to_provision_app(monkeypatch, tmp_path):
    """R2: the orchestrator threads a callable ``persist`` into provision_app
    so each resource ID can be written the moment it is created."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="full",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    mocks["logto"].assert_called_once()
    persist = mocks["logto"].call_args.kwargs.get("persist")
    assert callable(persist)


def test_orchestrator_persists_logto_ids_incrementally_on_midsequence_failure(
    monkeypatch, tmp_path
):
    """R2 / SC2: when provision_app fails mid-sequence (after creating the SPA
    and M2M), the IDs it already persisted via the callback survive in the
    state file even though STEP_LOGTO is never marked done — so ``--resume``
    can adopt them rather than orphan them."""
    mocks = _patch_all(monkeypatch, tmp_path)
    state_path = tmp_path / ".bootstrap-state.json"

    def _provision(*, persist=None, **kwargs):
        # Real provision_app persists each ID as its resource is created.
        assert persist is not None, "orchestrator must pass a persist callback"
        persist("logto_spa_client_id", "spa-live")
        persist("logto_m2m_app_id", "m2m-live")
        # ...then the API-resource create fails.
        raise RuntimeError("api resource create failed mid-sequence")

    mocks["logto"].side_effect = _provision

    with pytest.raises(SystemExit):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=False,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )

    raw = state_path.read_text()
    # IDs created before the failure are recorded (recoverable, not orphaned).
    assert "logto_spa_client_id" in raw and "spa-live" in raw
    assert "logto_m2m_app_id" in raw and "m2m-live" in raw
    # STEP_LOGTO must NOT be marked done — it failed mid-sequence.
    assert '"logto":true' not in raw
    assert '"logto": true' not in raw


# ---------------------------------------------------------------------------
# U5: Postgres provisioning step (STEP_POSTGRES) — gated on with_db, runs after
# STEP_RAILWAY_ENV, find-before-add so a resume never provisions a second DB,
# and never writes the secret-shaped DATABASE_URL to state.
# ---------------------------------------------------------------------------


def test_postgres_provisioned_when_with_db_and_no_existing_db(monkeypatch, tmp_path):
    """R4/SC3: with_db=True and no existing DB → add_database is called once."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_db=True,
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    # check() ran and saw no DB → add_database fired exactly once.
    mocks["railway_has_db"].assert_called_once()
    mocks["railway_add_db"].assert_called_once()
    # STEP_POSTGRES must also set the DATABASE_URL reference on the APP service
    # (Railway does NOT auto-inject it) — live-surfaced: without this the app
    # boots with no DATABASE_URL and crashes.
    db_ref_calls = [
        c
        for c in mocks["railway_env"].call_args_list
        if c.args and c.args[0] == "DATABASE_URL"
    ]
    assert len(db_ref_calls) == 1
    assert db_ref_calls[0].args[1] == "${{Postgres.DATABASE_URL}}"
    assert db_ref_calls[0].kwargs.get("service") == "foo"


def test_postgres_skipped_when_with_db_false(monkeypatch, tmp_path):
    """with_db=False → STEP_POSTGRES skipped entirely: no check, no add."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_db=False,
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    mocks["railway_has_db"].assert_not_called()
    mocks["railway_add_db"].assert_not_called()


def test_postgres_resume_with_existing_db_skips_add(monkeypatch, tmp_path):
    """SC2/Risk R-c: check() sees an existing DB → add_database is NOT called
    (no second Postgres on resume)."""
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        railway_has_db_result={"status": "success", "has_database": True},
    )
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_db=True,
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    mocks["railway_has_db"].assert_called_once()
    mocks["railway_add_db"].assert_not_called()


def test_postgres_add_failure_is_retryable_and_step_not_marked(monkeypatch, tmp_path):
    """A transient `railway add` failure routes through _fail as retryable and
    does NOT mark STEP_POSTGRES done, so --resume re-attempts."""
    _patch_all(
        monkeypatch,
        tmp_path,
        railway_add_db_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "add boom",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_db=True,
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    envelope = exc_info.value.envelope  # type: ignore[attr-defined]
    assert envelope["status"] == "failed"
    assert envelope["checkpoint"] == "postgres"
    assert envelope["retryable"] is True
    raw = state_path.read_text()
    # STEP_POSTGRES must NOT be marked done — it failed.
    assert '"postgres":true' not in raw
    assert '"postgres": true' not in raw


def test_postgres_marker_persisted_not_database_url(monkeypatch, tmp_path):
    """Entropy gate: a non-secret marker records that Postgres was provisioned,
    but no DATABASE_URL / secret-shaped connection string is ever written to
    state. We force a downstream (deploy) failure so the state file survives."""
    import re

    _patch_all(
        monkeypatch,
        tmp_path,
        railway_deploy_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "deploy boom",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_db=True,
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    raw = state_path.read_text()
    # A marker records provisioning happened...
    assert "railway_postgres" in raw
    # ...but no connection string / DATABASE_URL leaked into state.
    assert "DATABASE_URL" not in raw
    assert "postgres://" not in raw
    # And the entropy gate still holds (no 32+ char high-entropy run).
    entropy_re = re.compile(r"[A-Za-z0-9+/=_]{32,}")
    assert entropy_re.findall(raw) == []


# ---------------------------------------------------------------------------
# U6: STEP_WIRING — prod-domain CORS + SPA prod redirect URI (R4 / SC3).
# Runs after STEP_POSTGRES, before STEP_TEMPLATE/deploy, so the deployed app is
# correct on first request. Domain captured via `railway domain --json` (never
# decorated stdout). Redirect URIs MERGE (keep localhost). A silent no-op on
# the PATCH fails loudly, non-retryable (R8).
# ---------------------------------------------------------------------------


def test_wiring_sets_cors_and_merges_prod_redirect_uri(monkeypatch, tmp_path):
    """R4/SC3: STEP_WIRING captures the prod domain, sets CORS_ORIGINS to the
    prod origin, and PATCHes the SPA redirect URIs to add https://<domain>/callback."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="full",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    # Prod domain captured via --json helper (no decorated-stdout parsing).
    mocks["railway_prod_domain"].assert_called_once()

    # CORS_ORIGINS set to the prod origin.
    cors_calls = [
        c for c in mocks["railway_env"].call_args_list
        if (c.kwargs.get("name") or (c.args[0] if c.args else None)) == "CORS_ORIGINS"
    ]
    assert len(cors_calls) == 1
    cors_value = cors_calls[0].kwargs.get("value") or cors_calls[0].args[1]
    assert cors_value == "https://foo-production.up.railway.app"

    # Redirect URI PATCH called with the prod /callback.
    mocks["logto_redirect"].assert_called_once()
    kwargs = mocks["logto_redirect"].call_args.kwargs
    assert kwargs.get("spa_app_id") == "spa-abc"
    assert (
        kwargs.get("prod_redirect_uri")
        == "https://foo-production.up.railway.app/callback"
    )


def test_wiring_records_domain_in_state(monkeypatch, tmp_path):
    """The (non-secret) hostname is recorded in resource_ids. Force a downstream
    deploy failure so the state file survives for inspection."""
    import re

    _patch_all(
        monkeypatch,
        tmp_path,
        railway_deploy_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "deploy boom",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    raw = state_path.read_text()
    assert "railway_domain" in raw
    assert "foo-production.up.railway.app" in raw
    # Hostname is entropy-gate safe.
    entropy_re = re.compile(r"[A-Za-z0-9+/=_]{32,}")
    assert entropy_re.findall(raw) == []


def test_wiring_skipped_when_no_railway(monkeypatch, tmp_path):
    """No Railway → no prod domain exists → STEP_WIRING is skipped entirely."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    mocks["railway_prod_domain"].assert_not_called()
    mocks["logto_redirect"].assert_not_called()


def test_wiring_redirect_skipped_when_no_spa(monkeypatch, tmp_path):
    """static-dashboard --no-auth has no SPA → CORS is still set but no redirect
    PATCH is attempted (nothing to update)."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="static-dashboard",
        with_auth=False,
        with_logto=False,
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    mocks["railway_prod_domain"].assert_called_once()
    mocks["logto_redirect"].assert_not_called()
    # CORS is still wired for the deployed app.
    cors_calls = [
        c for c in mocks["railway_env"].call_args_list
        if (c.kwargs.get("name") or (c.args[0] if c.args else None)) == "CORS_ORIGINS"
    ]
    assert len(cors_calls) == 1


def test_wiring_silent_redirect_noop_fails_non_retryable(monkeypatch, tmp_path):
    """R8: if the PATCH silently no-ops (prod URI still absent afterward), the
    orchestrator fails loudly and non-retryably (structural — resume won't loop)."""
    from agentic_core.output_schema import FailureEnvelope

    _patch_all(
        monkeypatch,
        tmp_path,
        logto_redirect_result={
            # prod /callback is NOT present → silent no-op.
            "redirect_uris": ["http://localhost:5173/callback"],
            "patched": True,
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    model = FailureEnvelope.model_validate(envelope)
    assert model.checkpoint == "wiring"
    assert model.retryable is False
    raw = state_path.read_text()
    # STEP_WIRING must NOT be marked done.
    assert '"wiring":true' not in raw
    assert '"wiring": true' not in raw


def test_wiring_domain_capture_failure_is_retryable(monkeypatch, tmp_path):
    """A failed `railway domain` capture is transient → retryable, step not marked."""
    _patch_all(
        monkeypatch,
        tmp_path,
        railway_prod_domain_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "domain boom",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    envelope = exc_info.value.envelope  # type: ignore[attr-defined]
    assert envelope["checkpoint"] == "wiring"
    assert envelope["retryable"] is True
    raw = state_path.read_text()
    assert '"wiring":true' not in raw
    assert '"wiring": true' not in raw


def test_wiring_resume_adopts_spa_id_from_state(monkeypatch, tmp_path):
    """On resume STEP_LOGTO is skipped (logto_result is None), so the SPA app id
    for the redirect PATCH must come from resource_ids persisted earlier."""
    mocks = _patch_all(monkeypatch, tmp_path)
    state_path = tmp_path / ".bootstrap-state.json"
    pre_state = BootstrapState(
        app_slug="foo",
        variant="full",
        scaffolded_by="manual",
        created_at=datetime.now(tz=UTC),
        checkpoints={
            "keystore": True,
            "logto": True,
            "railway": True,
            "railway_env": True,
            "postgres": True,
        },
        resource_ids={
            "logto_spa_client_id": "spa-from-state",
            "railway_project_id": _RW_UUID,
        },
        last_completed_step="postgres",
    )
    from agentic_core.bootstrap import state as state_mod
    state_mod.write_state(state_path, pre_state)

    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        resume=True,
    )
    # provision_app was skipped (logto already done) but the redirect PATCH
    # still ran, using the SPA id adopted from state.
    mocks["logto"].assert_not_called()
    mocks["logto_redirect"].assert_called_once()
    assert mocks["logto_redirect"].call_args.kwargs.get("spa_app_id") == "spa-from-state"


# ---------------------------------------------------------------------------
# U7: STEP_VERIFY — final health + auth gate (R5 / R8 / KTD6).
# Runs AFTER STEP_RAILWAY_DEPLOY and BEFORE the success/state-delete. Only on
# verify success does the run reach state deletion. Structural verify failure
# (auth 401) → non-retryable so --resume refuses to loop on a broken app;
# transient failure (health never up) → retryable so resume re-checks later.
# ---------------------------------------------------------------------------


def test_verify_runs_after_deploy_and_deletes_state_on_success(monkeypatch, tmp_path):
    """Happy path: verify is called once after deploy; on success the state
    file is deleted (run completes)."""
    mocks = _patch_all(monkeypatch, tmp_path)
    state_path = tmp_path / ".bootstrap-state.json"
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
    )
    assert result["status"] == "success"
    mocks["railway_deploy"].assert_called_once()
    mocks["verify"].assert_called_once()
    # verify comes AFTER deploy: base_url is derived from the persisted domain.
    base_url = mocks["verify"].call_args.args[0]
    assert base_url == "https://foo-production.up.railway.app"
    assert "verify" in result["completed_steps"]
    # Success path deletes the state file.
    assert not state_path.exists()


def test_verify_receives_app_m2m_minter_on_fresh_run(monkeypatch, tmp_path):
    """On a fresh run (logto_result present) the orchestrator passes a callable
    mint_token built from the app's own provisioned M2M creds."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    mint = mocks["verify"].call_args.kwargs.get("mint_token")
    assert callable(mint)


def test_verify_structural_failure_is_non_retryable(monkeypatch, tmp_path):
    """R8/KTD6: a STRUCTURAL verify failure (auth rejected) → non-retryable so
    --resume refuses; state file survives, STEP_VERIFY not marked done."""
    from agentic_core.output_schema import FailureEnvelope

    _patch_all(
        monkeypatch,
        tmp_path,
        verify_result={
            "status": "structural",
            "error_code": "VERIFY_AUTH_REJECTED",
            "message": "deployed app rejected a valid app token (401)",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    model = FailureEnvelope.model_validate(envelope)
    assert model.checkpoint == "verify"
    assert model.error_code == "VERIFY_AUTH_REJECTED"
    assert model.retryable is False
    # State survives for inspection; STEP_VERIFY NOT marked done.
    assert state_path.exists()
    raw = state_path.read_text()
    assert '"verify":true' not in raw
    assert '"verify": true' not in raw


def test_verify_transient_failure_is_retryable(monkeypatch, tmp_path):
    """R5/KTD6: a TRANSIENT verify failure (health never 200 in bound) →
    retryable so --resume re-checks later (slow rollout). Step not marked."""
    _patch_all(
        monkeypatch,
        tmp_path,
        verify_result={
            "status": "transient",
            "error_code": "VERIFY_HEALTH_UNAVAILABLE",
            "message": "/health never returned 200 within the bound",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    envelope = exc_info.value.envelope  # type: ignore[attr-defined]
    assert envelope["checkpoint"] == "verify"
    assert envelope["retryable"] is True
    raw = state_path.read_text()
    assert '"verify":true' not in raw
    assert '"verify": true' not in raw


def test_verify_skipped_when_no_railway(monkeypatch, tmp_path):
    """No Railway → nothing deployed → STEP_VERIFY is skipped entirely."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    mocks["verify"].assert_not_called()


def test_verify_runs_after_deploy_marked_done(monkeypatch, tmp_path):
    """Integration: verify runs only after the deploy step is marked done —
    a deploy failure short-circuits before verify is ever reached."""
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        railway_deploy_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "deploy boom",
        },
    )
    with pytest.raises(SystemExit):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=tmp_path / ".bootstrap-state.json",
            scaffolded_by="manual",
        )
    # Deploy failed → verify never runs.
    mocks["verify"].assert_not_called()


# ---------------------------------------------------------------------------
# Fix 2 (P1): STEP_WIRING cold-cache deadlock. A LogtoAuthError from the
# redirect-URI PATCH means the admin mgmt credential is gone from memory
# mid-resume — retrying can NEVER succeed, so it must be non-retryable (not
# swept into the general LogtoError retryable catch).
# ---------------------------------------------------------------------------


def test_wiring_logto_auth_error_is_non_retryable(monkeypatch, tmp_path):
    """Fix 2: a ``LogtoAuthError`` (stale mgmt-token cache, admin credential
    unrecoverable mid-resume) raised by ``update_app_redirect_uris`` in
    STEP_WIRING must fail loud NON-retryable — a bare ``LogtoError`` would be
    retryable, but this one can never succeed on a re-run."""
    from agentic_core.output_schema import FailureEnvelope

    mocks = _patch_all(monkeypatch, tmp_path)
    mocks["logto_redirect"].side_effect = orchestrator.logto.LogtoAuthError(
        "management token expired; admin credential no longer in memory"
    )
    state_path = tmp_path / ".bootstrap-state.json"
    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    model = FailureEnvelope.model_validate(envelope)
    assert model.checkpoint == "wiring"
    assert model.retryable is False
    # STEP_WIRING must NOT be marked done, and deploy/verify never ran.
    raw = state_path.read_text()
    assert '"wiring":true' not in raw and '"wiring": true' not in raw
    mocks["railway_deploy"].assert_not_called()
    mocks["verify"].assert_not_called()


def test_wiring_generic_logto_error_stays_retryable(monkeypatch, tmp_path):
    """Fix 2 boundary: a non-auth ``LogtoError`` from the redirect PATCH stays
    retryable (transient API blip) — the non-retryable path is reserved for
    ``LogtoAuthError`` only."""
    mocks = _patch_all(monkeypatch, tmp_path)
    mocks["logto_redirect"].side_effect = orchestrator.logto.LogtoError(
        "transient 503 from Logto"
    )
    state_path = tmp_path / ".bootstrap-state.json"
    with pytest.raises(SystemExit) as exc_info:
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=True,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=state_path,
            scaffolded_by="manual",
        )
    envelope = exc_info.value.envelope  # type: ignore[attr-defined]
    assert envelope["checkpoint"] == "wiring"
    assert envelope["retryable"] is True


# ---------------------------------------------------------------------------
# Fix 4 (P2): STEP_VERIFY must not silently mark done when with_railway=True
# but no prod domain is recorded — that is the silent-success class R5 kills.
# ---------------------------------------------------------------------------


def test_verify_no_domain_fails_loud_non_retryable(monkeypatch, tmp_path):
    """Fix 4: with_railway=True but no ``railway_domain`` in state → verify must
    NOT fall through and mark itself done without any health/auth check. It
    fails loud, non-retryable (no prod domain to verify against)."""
    from agentic_core.output_schema import FailureEnvelope

    mocks = _patch_all(monkeypatch, tmp_path)
    state_path = tmp_path / ".bootstrap-state.json"
    # Everything through deploy is done; verify is the only pending step. Crucially
    # resource_ids has NO railway_domain (models a state where wiring's domain
    # persistence was lost / never happened).
    pre_state = BootstrapState(
        app_slug="foo",
        variant="full",
        scaffolded_by="manual",
        created_at=datetime.now(tz=UTC),
        checkpoints={
            "keystore": True,
            "logto": True,
            "railway": True,
            "railway_env": True,
            "postgres": True,
            "wiring": True,
            "template": True,
            "railway_deploy": True,
        },
        resource_ids={"logto_spa_client_id": "spa-abc"},  # NOTE: no railway_domain
        last_completed_step="railway_deploy",
    )
    from agentic_core.bootstrap import state as state_mod
    state_mod.write_state(state_path, pre_state)

    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        resume=True,
        raise_on_failure=False,
    )
    model = FailureEnvelope.model_validate(envelope)
    assert model.checkpoint == "verify"
    assert model.error_code == "VERIFY_NO_DOMAIN"
    assert model.retryable is False
    # No probe was attempted, and verify was NOT marked done.
    mocks["verify"].assert_not_called()
    raw = state_path.read_text()
    assert '"verify":true' not in raw and '"verify": true' not in raw


# ---------------------------------------------------------------------------
# Fix #2 — deployed backend env-var contract. The template's Settings
# (config.py.jinja, NO env_prefix) + JWT middleware read UNPREFIXED names by
# field name (LOGTO_ENDPOINT, LOGTO_API_RESOURCE, LOGTO_M2M_AGENT_APP_ID,
# LOGTO_M2M_AGENT_APP_SECRET, LOGTO_APP_ID). STEP_RAILWAY_ENV must push those in
# ADDITION to the <APP>_-prefixed CLI vars, or the deployed backend 503s.
# ---------------------------------------------------------------------------


def _env_map(env_mock):
    """Collapse set_env_var calls into a {name: value} dict."""
    out: dict[str, str | None] = {}
    for c in env_mock.call_args_list:
        name = c.kwargs.get("name") or (c.args[0] if c.args else None)
        value = c.kwargs.get("value") or (c.args[1] if len(c.args) > 1 else None)
        out[name] = value
    return out


def test_railway_env_pushes_unprefixed_backend_vars_fresh(monkeypatch, tmp_path):
    """Fix #2: on a fresh run STEP_RAILWAY_ENV pushes the UNPREFIXED backend
    names the deployed FastAPI Settings + middleware actually read, sourced from
    logto_result — alongside (not instead of) the prefixed CLI vars."""
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    env = _env_map(mocks["railway_env"])
    # Unprefixed backend names (config.py reads these by field name):
    assert env.get("LOGTO_ENDPOINT") == "https://logto.test"
    assert env.get("LOGTO_API_RESOURCE") == "https://foo.example.test/api"
    assert env.get("LOGTO_M2M_AGENT_APP_ID") == "m2m-abc"
    assert env.get("LOGTO_M2M_AGENT_APP_SECRET") == "secret-abc"
    assert env.get("LOGTO_APP_ID") == "spa-abc"
    # Prefixed CLI vars are STILL present (client.py reads these) — not removed.
    assert env.get("FOO_M2M_APP_ID") == "m2m-abc"
    assert env.get("FOO_LOGTO_ENDPOINT") == "https://logto.test"


def test_railway_env_pushes_unprefixed_backend_vars_on_resume(monkeypatch, tmp_path):
    """Fix #2 (resume): static-dashboard has NO M2M secret, so a resume past
    STEP_LOGTO can reach STEP_RAILWAY_ENV. The unprefixed NON-secret backend
    vars are still pushed (sourced from persisted state); the secret one is
    skipped (it's gone — the known unrecoverable limitation)."""
    state_path = tmp_path / ".bootstrap-state.json"
    pre_state = BootstrapState(
        app_slug="foo",
        variant="static-dashboard",
        scaffolded_by="manual",
        created_at=datetime.now(tz=UTC),
        checkpoints={"keystore": True, "logto": True},
        resource_ids={
            "logto_spa_client_id": "spa-live",
            "logto_user_sub": "app_foo",
        },
        last_completed_step="logto",
    )
    from agentic_core.bootstrap import state as state_mod
    state_mod.write_state(state_path, pre_state)

    mocks = _patch_all(monkeypatch, tmp_path)
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="static-dashboard",
        with_auth=True,
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        resume=True,
    )
    assert result["status"] == "success"
    env = _env_map(mocks["railway_env"])
    # Unprefixed backend vars pushed from persisted state on resume.
    assert env.get("LOGTO_APP_ID") == "spa-live"
    assert "LOGTO_ENDPOINT" in env
    # The secret is gone on resume — never pushed (unrecoverable, accepted).
    assert "LOGTO_M2M_AGENT_APP_SECRET" not in env


# ---------------------------------------------------------------------------
# Fix #12 — 3-tier endpoint resolution (arg → LOGTO_ENDPOINT env → domain
# default) must be used at EVERY orchestrator site so provisioning and wiring
# target the same endpoint an operator set via env.
# ---------------------------------------------------------------------------


def test_logto_endpoint_env_used_for_provisioning_and_wiring(monkeypatch, tmp_path):
    """Fix #12: with LOGTO_ENDPOINT set and no --logto-endpoint, both the Logto
    PROVISION call and the STEP_WIRING redirect PATCH target the env value —
    previously provision used a 2-tier fallback that ignored the env var."""
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://env-logto.example")
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    assert mocks["logto"].call_args.kwargs.get("endpoint") == "https://env-logto.example"
    assert (
        mocks["logto_redirect"].call_args.kwargs.get("endpoint")
        == "https://env-logto.example"
    )


# ---------------------------------------------------------------------------
# LogtoSecretUnrecoverable at STEP_LOGTO → non-retryable (Wave-1 new type).
# ---------------------------------------------------------------------------


def test_logto_secret_unrecoverable_is_non_retryable(monkeypatch, tmp_path):
    """A ``LogtoSecretUnrecoverable`` from provision_app must be classified
    non-retryable (like the other unrecoverable-secret paths), NOT swept into
    the generic retryable ``LogtoError`` handler."""
    from agentic_core.output_schema import FailureEnvelope

    mocks = _patch_all(monkeypatch, tmp_path)
    mocks["logto"].side_effect = orchestrator.logto.LogtoSecretUnrecoverable(
        "M2M secret not recoverable from Logto"
    )
    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    model = FailureEnvelope.model_validate(envelope)
    assert model.checkpoint == "logto"
    assert model.error_code == "LOGTO_SECRET_UNRECOVERABLE"
    assert model.retryable is False


# ---------------------------------------------------------------------------
# Fix #4 — the M2M secret must never appear in the success envelope (--json
# stdout). The logto sub-block is sanitized of secret-shaped keys.
# ---------------------------------------------------------------------------


def test_success_envelope_strips_m2m_secret(monkeypatch, tmp_path):
    """Fix #4: the success envelope's ``logto`` block carries no ``m2m_app_secret``
    (nor any ``*_secret`` key), and the secret value appears nowhere in the
    serialized envelope."""
    import json as _json

    _patch_all(monkeypatch, tmp_path)
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
    )
    assert result["status"] == "success"
    assert "m2m_app_secret" not in result["logto"]
    assert all(not k.endswith("_secret") for k in result["logto"])
    assert "secret-abc" not in _json.dumps(result)
    # The non-secret IDs are still present (envelope stays useful).
    assert result["logto"]["m2m_app_id"] == "m2m-abc"


# ---------------------------------------------------------------------------
# Fix #9 — the R8 entropy gate (StateFileEntropyError) must NOT be swallowed by
# the best-effort write_state try/except in _mark / _persist_logto_id / _fail.
# A secret about to hit disk aborts loudly.
# ---------------------------------------------------------------------------


def test_mark_propagates_state_file_entropy_error(monkeypatch, tmp_path):
    """Fix #9: a StateFileEntropyError raised by write_state inside ``_mark``
    propagates instead of being silently swallowed."""
    from agentic_core.bootstrap import state as state_mod

    _patch_all(monkeypatch, tmp_path)

    def _boom(_path, _obj):
        raise state_mod.StateFileEntropyError("high-entropy secret would hit disk")

    monkeypatch.setattr(orchestrator.state, "write_state", _boom)
    with pytest.raises(state_mod.StateFileEntropyError):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_logto=False,
            with_railway=False,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=tmp_path / ".bootstrap-state.json",
            scaffolded_by="manual",
        )


def test_persist_logto_id_propagates_state_file_entropy_error(monkeypatch, tmp_path):
    """Fix #9: a StateFileEntropyError from write_state inside the
    ``_persist_logto_id`` callback propagates (not masked as LOGTO_UNEXPECTED)."""
    from agentic_core.bootstrap import state as state_mod

    mocks = _patch_all(monkeypatch, tmp_path)
    real_write = state_mod.write_state

    def _flaky(path, obj):
        if obj.resource_ids.get("logto_spa_client_id"):
            raise state_mod.StateFileEntropyError("secret would hit disk")
        return real_write(path, obj)

    monkeypatch.setattr(orchestrator.state, "write_state", _flaky)

    def _provision(*, persist=None, **kwargs):
        assert persist is not None
        persist("logto_spa_client_id", "spa-live")  # triggers the entropy raise
        return {"spa_client_id": "spa-live"}

    mocks["logto"].side_effect = _provision

    with pytest.raises(state_mod.StateFileEntropyError):
        orchestrator.orchestrate_create_app(
            slug="foo",
            description="x",
            with_railway=False,
            with_github=False,
            keystore_path=tmp_path / "admin.keystore.json",
            output_dir=tmp_path / "out",
            state_path=tmp_path / ".bootstrap-state.json",
            scaffolded_by="manual",
        )


# ---------------------------------------------------------------------------
# Fix #3 — a resumed health-only verify must NOT present itself as fully
# auth-verified. auth_checked=False is surfaced in what the orchestrator records.
# ---------------------------------------------------------------------------


def test_resume_health_only_verify_reports_auth_not_checked(monkeypatch, tmp_path):
    """Fix #3: on resume STEP_LOGTO is skipped (logto_result None) → verify runs
    health-only (mint_token None) → auth_checked False. That honesty must reach
    the success envelope so the run doesn't claim auth was re-verified."""
    state_path = tmp_path / ".bootstrap-state.json"
    pre_state = BootstrapState(
        app_slug="foo",
        variant="full",
        scaffolded_by="manual",
        created_at=datetime.now(tz=UTC),
        checkpoints={
            "keystore": True,
            "logto": True,
            "railway": True,
            "railway_env": True,
            "postgres": True,
            "wiring": True,
            "template": True,
            "railway_deploy": True,
        },
        resource_ids={
            "logto_spa_client_id": "spa-abc",
            "railway_domain": "foo-production.up.railway.app",
        },
        last_completed_step="railway_deploy",
    )
    from agentic_core.bootstrap import state as state_mod
    state_mod.write_state(state_path, pre_state)

    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        verify_result={"status": "success", "auth_checked": False},
    )
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        resume=True,
    )
    assert result["status"] == "success"
    # Health-only: no app token minter was passed.
    assert mocks["verify"].call_args.kwargs.get("mint_token") is None
    # The run is honest that auth was NOT re-checked.
    assert result["verify"]["auth_checked"] is False


# ---------------------------------------------------------------------------
# U7: Render deploy target (KTD2, KTD8, KTD11, KTD12)
# ---------------------------------------------------------------------------

_RENDER_PREFLIGHT_OK = {
    "status": "success",
    "checkpoint": "preflight",
    "render_workspace_id": "tea-abc123",
    "render_workspace_name": "My Workspace",
}

#: Every Railway-provider mock in ``_patch_all``.
_RAILWAY_MOCKS = (
    "railway",
    "railway_service",
    "railway_linked",
    "railway_has_db",
    "railway_add_db",
    "railway_env",
    "railway_link",
    "railway_deploy",
    "railway_wait",
    "railway_prod_domain",
    "verify",
)

#: Every keystore and Logto mock in ``_patch_all``.
_SECRET_MOCKS = (
    "decrypt",
    "load_keystore",
    "read_passphrase",
    "resolve_path",
    "logto",
    "logto_redirect",
    "github_secret",
)


def _render_run(tmp_path, **overrides):
    kwargs = {
        "slug": "foo",
        "description": "x",
        "deploy_target": "render",
        "output_dir": tmp_path / "out",
        "state_path": tmp_path / ".bootstrap-state.json",
        "scaffolded_by": "manual",
    }
    kwargs.update(overrides)
    return orchestrator.orchestrate_create_app(**kwargs)


def test_render_target_never_touches_keystore_logto_or_railway(monkeypatch, tmp_path):
    """Render target with default with_logto/with_railway: no keystore, no
    Logto, no Railway call. Auth is reported as not configured."""
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    result = _render_run(tmp_path)

    assert result["status"] == "success"
    for name in _SECRET_MOCKS + _RAILWAY_MOCKS:
        mocks[name].assert_not_called()
    assert result["auth"]["status"] == "not_configured"
    assert result["auth"]["next_step"]
    assert result["railway"] is None
    assert result["deploy_target"] == "render"


def test_render_target_success_envelope_and_step_order(monkeypatch, tmp_path):
    """Preflight -> template -> validate -> GitHub repo -> push, then a
    success envelope that points at the Blueprint apply link."""
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    manager = mock.Mock()
    manager.attach_mock(mocks["preflight"], "preflight")
    manager.attach_mock(mocks["render_template"], "template")
    manager.attach_mock(mocks["render_validate"], "validate")
    manager.attach_mock(mocks["github"], "github")
    manager.attach_mock(mocks["github_push"], "push")

    result = _render_run(tmp_path)

    names = [c[0] for c in manager.mock_calls]
    assert names == ["preflight", "template", "validate", "github", "push"]
    assert result["completed_steps"] == [
        "template",
        "render_validate",
        "github",
        "git_commit",
    ]
    render_block = result["render"]
    assert render_block["status"] == "awaiting_blueprint_apply"
    assert render_block["validated"] is True
    assert render_block["workspace"] == "My Workspace"
    assert render_block["workspace_id"] == "tea-abc123"
    assert render_block["blueprint_path"] == str(tmp_path / "out" / "foo" / "render.yaml")
    assert render_block["blueprint_url"] == (
        "https://render.com/deploy?repo=https://github.com/example-owner/foo"
    )
    assert not (tmp_path / ".bootstrap-state.json").exists()


def test_render_target_passes_target_to_preflight_copier_and_validate(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    _render_run(tmp_path, with_github=False)

    pf_kwargs = mocks["preflight"].call_args.kwargs
    assert pf_kwargs["deploy_target"] == "render"
    assert pf_kwargs["with_deploy"] is True
    assert pf_kwargs["with_logto"] is False
    data = mocks["render_template"].call_args.kwargs["data"]
    assert data["deploy_target"] == "render"
    assert data["with_auth"] is False
    v_args = mocks["render_validate"].call_args
    assert v_args.args[0] == str(tmp_path / "out" / "foo" / "render.yaml")
    assert v_args.args[1] == "tea-abc123"
    assert v_args.kwargs["cwd"] == str(tmp_path / "out" / "foo")


def test_render_target_failed_validation_creates_no_github_repo(monkeypatch, tmp_path):
    """An invalid Blueprint stops the run before GitHub: no repo, no commit,
    non-retryable, and the state file stays for inspection."""
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        preflight_result=_RENDER_PREFLIGHT_OK,
        render_validate_result={
            "status": "failed",
            "error_code": "RENDER_BLUEPRINT_INVALID",
            "exit_code": 1,
            "error_message": "services[0].plan: unknown plan",
            "retryable": False,
            "errors": [{"path": "services[0].plan", "error": "unknown plan"}],
        },
    )
    result = _render_run(tmp_path, raise_on_failure=False)

    assert result["status"] == "failed"
    assert result["checkpoint"] == "render_validate"
    assert result["error_code"] == "RENDER_BLUEPRINT_INVALID"
    assert result["retryable"] is False
    mocks["github"].assert_not_called()
    mocks["github_push"].assert_not_called()
    assert (tmp_path / ".bootstrap-state.json").exists()


def test_render_target_transient_validation_failure_is_retryable(monkeypatch, tmp_path):
    """Retryability comes from the wrapper's own classification (5xx)."""
    _patch_all(
        monkeypatch,
        tmp_path,
        preflight_result=_RENDER_PREFLIGHT_OK,
        render_validate_result={
            "status": "failed",
            "error_code": "RENDER_VALIDATE_FAILED",
            "exit_code": 1,
            "error_message": "Error: validation request failed with status 503",
            "retryable": True,
        },
    )
    result = _render_run(tmp_path, raise_on_failure=False)
    assert result["error_code"] == "RENDER_VALIDATE_FAILED"
    assert result["retryable"] is True


def test_render_target_without_github_has_null_blueprint_url_and_next_step(
    monkeypatch, tmp_path
):
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    result = _render_run(tmp_path, with_github=False)

    assert result["status"] == "success"
    mocks["github"].assert_not_called()
    assert result["github"] is None
    assert result["render"]["blueprint_url"] is None
    next_step = result["render"]["next_step"]
    assert "render.com/deploy" in next_step
    assert "New > Blueprint" in next_step


def test_render_target_without_deploy_skips_render_calls(monkeypatch, tmp_path):
    """with_deploy=False: template still renders, no Render validation."""
    mocks = _patch_all(monkeypatch, tmp_path)
    result = _render_run(tmp_path, with_deploy=False, with_github=False)

    assert result["status"] == "success"
    assert mocks["preflight"].call_args.kwargs["with_deploy"] is False
    mocks["render_validate"].assert_not_called()
    mocks["render_template"].assert_called_once()
    assert result["render"]["validated"] is False
    assert result["render"]["workspace_id"] is None


def test_render_progress_reports_validate_step(monkeypatch, tmp_path):
    _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    seen: list[str] = []
    _render_run(tmp_path, with_github=False, progress=seen.append)
    joined = "\n".join(seen)
    assert "Validating render.yaml" in joined
    assert "Render Blueprint validated" in joined


def test_render_dry_run_names_render_steps_and_runs_no_subprocess(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)

    def _no_subprocess(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess call: {args!r}")

    monkeypatch.setattr("subprocess.run", _no_subprocess)
    result = _render_run(tmp_path, dry_run=True)

    assert result["status"] == "success"
    assert result["dry_run"] is True
    assert result["planned_steps"] == [
        "preflight",
        "template",
        "render_validate",
        "github",
        "git_commit",
    ]
    assert result["railway"] is None
    assert result["render"]["status"] == "dry_run"
    assert result["auth"]["status"] == "not_configured"
    mocks["preflight"].assert_not_called()
    mocks["render_template"].assert_not_called()


def test_new_state_records_deploy_target(monkeypatch, tmp_path):
    _patch_all(
        monkeypatch,
        tmp_path,
        preflight_result=_RENDER_PREFLIGHT_OK,
        render_validate_result={
            "status": "failed",
            "error_code": "RENDER_BLUEPRINT_INVALID",
            "exit_code": 1,
            "error_message": "bad",
            "retryable": False,
        },
    )
    _render_run(tmp_path, raise_on_failure=False)
    from agentic_core.bootstrap import state as state_mod

    saved = state_mod.read_state(tmp_path / ".bootstrap-state.json")
    assert saved.deploy_target == "render"
    assert saved.auth_provider == "google"


def test_resume_with_different_deploy_target_fails_and_keeps_state(monkeypatch, tmp_path):
    """KTD8: an old (Railway) state file resumed with the Render target fails
    non-retryable and does not change the state file."""
    state_path = tmp_path / ".bootstrap-state.json"
    state_path.write_text(
        '{"schema_version": 1, "app_slug": "foo", "variant": "full", '
        '"scaffolded_by": "manual", "created_at": "2026-11-01T00:00:00Z", '
        '"checkpoints": {"keystore": true}, "resource_ids": {}, '
        '"last_completed_step": "keystore"}'
    )
    before = state_path.read_text()
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)

    result = _render_run(tmp_path, resume=True, raise_on_failure=False)

    assert result["status"] == "failed"
    assert result["error_code"] == "DEPLOY_TARGET_MISMATCH"
    assert result["retryable"] is False
    assert state_path.read_text() == before
    mocks["render_template"].assert_not_called()
    mocks["render_validate"].assert_not_called()


@pytest.mark.parametrize(
    ("recorded", "requested"), [("railway", "render"), ("render", "railway")]
)
def test_resume_target_mismatch_fails_before_preflight(
    monkeypatch, tmp_path, recorded, requested
):
    """The mismatch shows before the other target's preflight checks can
    fail with an unrelated CLI or credential error."""
    state_path = tmp_path / ".bootstrap-state.json"
    state_path.write_text(
        '{"schema_version": 1, "app_slug": "foo", "variant": "full", '
        '"scaffolded_by": "manual", "created_at": "2026-11-01T00:00:00Z", '
        '"checkpoints": {"template": true}, "resource_ids": {}, '
        f'"last_completed_step": "template", "deploy_target": "{recorded}"}}'
    )
    before = state_path.read_text()
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)

    result = _render_run(
        tmp_path, deploy_target=requested, resume=True, raise_on_failure=False
    )

    assert result["error_code"] == "DEPLOY_TARGET_MISMATCH"
    assert result["exit_code"] == 1
    assert result["retryable"] is False
    assert result["completed_steps"] == ["template"]
    mocks["preflight"].assert_not_called()
    assert state_path.read_text() == before


@pytest.mark.parametrize("target", ["render", "railway"])
def test_invalid_variant_is_user_error_before_preflight(monkeypatch, tmp_path, target):
    """A bad --variant is an R4 user error (exit 1) on every target. The
    Render target skips Logto, so the Logto flag mapping cannot catch it."""
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)

    result = _render_run(
        tmp_path, deploy_target=target, variant="bogus", raise_on_failure=False
    )

    assert result["status"] == "failed"
    assert result["error_code"] == "INVALID_VARIANT"
    assert result["exit_code"] == 1
    assert result["retryable"] is False
    mocks["preflight"].assert_not_called()
    mocks["render_template"].assert_not_called()
    assert not (tmp_path / ".bootstrap-state.json").exists()


def test_render_github_failure_after_validation_keeps_state(monkeypatch, tmp_path):
    """A GitHub failure after a good validation is retryable. The saved
    state records the validation and the run can resume."""
    _patch_all(
        monkeypatch,
        tmp_path,
        preflight_result=_RENDER_PREFLIGHT_OK,
        github_result={
            "status": "failed",
            "error_code": "GH_CLI_ERROR",
            "exit_code": 1,
            "error_message": "gh: network error",
        },
    )
    result = _render_run(tmp_path, raise_on_failure=False)

    assert result["status"] == "failed"
    assert result["checkpoint"] == "github"
    assert result["retryable"] is True
    from agentic_core.bootstrap import state as state_mod

    saved = state_mod.read_state(tmp_path / ".bootstrap-state.json")
    assert saved.checkpoints.get("render_validate") is True
    assert not saved.checkpoints.get("github")


def test_render_resume_before_push_validates_blueprint_again(monkeypatch, tmp_path):
    """#4: until the push is done, a resume validates render.yaml again. The
    file or the active workspace can change between the runs."""
    state_path = tmp_path / ".bootstrap-state.json"
    state_path.write_text(
        '{"schema_version": 1, "app_slug": "foo", "variant": "full", '
        '"scaffolded_by": "manual", "created_at": "2026-11-01T00:00:00Z", '
        '"checkpoints": {"template": true, "render_validate": true}, '
        '"resource_ids": {}, "last_completed_step": "render_validate", '
        '"last_failure_retryable": true, "last_failure_code": "GH_CLI_ERROR", '
        '"deploy_target": "render"}'
    )
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    manager = mock.Mock()
    manager.attach_mock(mocks["render_validate"], "validate")
    manager.attach_mock(mocks["github"], "github")
    manager.attach_mock(mocks["github_push"], "push")

    result = _render_run(tmp_path, resume=True)

    assert result["status"] == "success"
    names = [c[0] for c in manager.mock_calls]
    assert names == ["validate", "github", "push"]
    mocks["render_template"].assert_not_called()
    assert result["render"]["validated"] is True
    assert result["render"]["workspace_id"] == "tea-abc123"
    assert result["completed_steps"].count("render_validate") == 1


def test_render_resume_revalidation_failure_stops_before_github(monkeypatch, tmp_path):
    """A resume whose new validation fails creates no GitHub repo."""
    state_path = tmp_path / ".bootstrap-state.json"
    state_path.write_text(
        '{"schema_version": 1, "app_slug": "foo", "variant": "full", '
        '"scaffolded_by": "manual", "created_at": "2026-11-01T00:00:00Z", '
        '"checkpoints": {"template": true, "render_validate": true}, '
        '"resource_ids": {}, "last_completed_step": "render_validate", '
        '"deploy_target": "render"}'
    )
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        preflight_result=_RENDER_PREFLIGHT_OK,
        render_validate_result={
            "status": "failed",
            "error_code": "RENDER_BLUEPRINT_INVALID",
            "exit_code": 1,
            "error_message": "services[0].plan: unknown plan",
            "retryable": False,
        },
    )

    result = _render_run(tmp_path, resume=True, raise_on_failure=False)

    assert result["error_code"] == "RENDER_BLUEPRINT_INVALID"
    mocks["github"].assert_not_called()
    mocks["github_push"].assert_not_called()


def test_default_arguments_keep_railway_envelope(monkeypatch, tmp_path):
    """KTD12: the default Python API stays on Railway and its envelope keeps
    the railway block."""
    mocks = _patch_all(monkeypatch, tmp_path)
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        railway_workspace="agent-sandbox",
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
    )
    assert result["status"] == "success"
    assert result["railway"]["workspace"] == "agent-sandbox"
    assert result["deploy_target"] == "railway"
    mocks["render_validate"].assert_not_called()
    assert mocks["preflight"].call_args.kwargs["deploy_target"] == "railway"
    assert mocks["render_template"].call_args.kwargs["data"]["deploy_target"] == "railway"


# ---------------------------------------------------------------------------
# Sign-in provider selection
# ---------------------------------------------------------------------------


def test_render_target_renders_google_sign_in_without_mobile(monkeypatch, tmp_path):
    """Render apps get Google sign-in. Google has no mobile client yet, so the
    mobile app is dropped even when mobile was requested."""
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    _render_run(tmp_path, variant="full", with_mobile=True)

    copier_data = mocks["render_template"].call_args.kwargs["data"]
    assert copier_data["auth_provider"] == "google"
    assert copier_data["include_mobile"] is False


def test_legacy_python_railway_default_keeps_logto_and_mobile(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    orchestrator.orchestrate_create_app(
        slug="foo",
        variant="full",
        with_mobile=True,
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )

    copier_data = mocks["render_template"].call_args.kwargs["data"]
    assert copier_data["auth_provider"] == "logto"
    assert copier_data["include_mobile"] is True


def test_render_auth_block_names_settings_and_limitations(monkeypatch, tmp_path):
    _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    result = _render_run(tmp_path)

    auth = result["auth"]
    assert auth["provider"] == "google_oidc"
    assert auth["status"] == "not_configured"
    assert auth["required_settings"] == [
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "AUTH_ALLOWED_EMAILS",
    ]
    assert any("mobile" in line.lower() for line in auth["limitations"])
    assert "/auth/callback" in auth["next_step"]
    # Names only: the block never carries a value for any setting.
    assert "=" not in json.dumps(auth)


def test_render_dry_run_reports_the_same_auth_block(monkeypatch, tmp_path):
    _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    real = _render_run(tmp_path)["auth"]
    dry = _render_run(tmp_path, dry_run=True)["auth"]
    assert dry == real

def test_railway_google_deploys_without_contacting_logto(monkeypatch, tmp_path):
    mocks = _patch_all(
        monkeypatch, tmp_path, verify_result={"status": "success", "auth_checked": False}
    )
    result = _render_run(tmp_path, deploy_target="railway", auth_provider="google")

    assert result["status"] == "success"
    for name in (*_SECRET_MOCKS, "logto_redirect"):
        mocks[name].assert_not_called()
    mocks["railway_deploy"].assert_called_once()
    assert mocks["verify"].call_args.kwargs["mint_token"] is None
    assert result["auth"]["provider"] == "google_oidc"
    assert "APP_BASE_URL" in result["auth"]["required_settings"]
    assert mocks["render_template"].call_args.kwargs["data"]["auth_provider"] == "google"


def test_render_logto_renders_without_provisioning_credentials(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path, preflight_result=_RENDER_PREFLIGHT_OK)
    result = _render_run(tmp_path, auth_provider="logto")

    assert result["status"] == "success"
    assert result["auth"]["provider"] == "logto"
    assert result["auth"]["status"] == "not_configured"
    for name in (*_SECRET_MOCKS, *_RAILWAY_MOCKS):
        mocks[name].assert_not_called()
    data = mocks["render_template"].call_args.kwargs["data"]
    assert data["auth_provider"] == "logto"
    assert data["include_mobile"] is False


def test_resume_refuses_to_switch_auth_provider_before_preflight(monkeypatch, tmp_path):
    state_path = tmp_path / ".bootstrap-state.json"
    before = json.dumps({
        "app_slug": "foo",
        "variant": "full",
        "scaffolded_by": "manual",
        "created_at": "2026-11-01T00:00:00Z",
        "checkpoints": {"template": True},
        "last_completed_step": "template",
        "deploy_target": "railway",
        "auth_provider": "google",
    })
    state_path.write_text(before)
    mocks = _patch_all(monkeypatch, tmp_path)

    result = _render_run(
        tmp_path, deploy_target="railway", auth_provider="logto",
        resume=True, raise_on_failure=False,
    )

    assert result["error_code"] == "AUTH_PROVIDER_MISMATCH"
    mocks["preflight"].assert_not_called()
    assert state_path.read_text() == before


def test_default_output_dir_is_the_checkout_parent():
    """New apps are siblings of the checkout, the folder list-apps scans."""
    from agentic_core import list_apps

    repo_root = Path(orchestrator.__file__).resolve().parents[2]
    assert (repo_root / "templates" / "mini-app" / "copier.yml").is_file()
    assert orchestrator._default_output_dir() == repo_root.parent
    assert orchestrator._default_output_dir() == list_apps._resolve_sibling_root()
    assert orchestrator.default_state_path("foo", None) == (
        repo_root.parent / "foo" / ".bootstrap-state.json"
    )


def _create_app_with_github(tmp_path):
    orchestrator.orchestrate_create_app(
        slug="foo",
        with_railway=False,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )


def test_github_owner_defaults_to_the_logged_in_gh_account(monkeypatch, tmp_path):
    """Other people run this CLI: never assume the maintainer's GitHub account."""
    mocks = _patch_all(monkeypatch, tmp_path)
    monkeypatch.delenv("GITHUB_USER", raising=False)
    monkeypatch.setattr(orchestrator.github, "logged_in_login", lambda timeout=15: "friend-user")

    _create_app_with_github(tmp_path)

    assert mocks["github"].call_args.kwargs["name"] == "friend-user/foo"
    assert mocks["render_template"].call_args.kwargs["data"]["github_user"] == "friend-user"


def test_github_user_env_wins_over_the_gh_account(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_USER", "my-org")
    monkeypatch.setattr(orchestrator.github, "logged_in_login", lambda timeout=15: "friend-user")

    _create_app_with_github(tmp_path)

    assert mocks["github"].call_args.kwargs["name"] == "my-org/foo"


def test_unknown_github_owner_passes_a_bare_repo_name(monkeypatch, tmp_path):
    """gh creates a bare name under the logged-in account."""
    mocks = _patch_all(monkeypatch, tmp_path)
    monkeypatch.delenv("GITHUB_USER", raising=False)
    monkeypatch.setattr(orchestrator.github, "logged_in_login", lambda timeout=15: None)

    _create_app_with_github(tmp_path)

    assert mocks["github"].call_args.kwargs["name"] == "foo"
    assert mocks["render_template"].call_args.kwargs["data"]["github_user"] == ""


def test_no_github_run_never_calls_gh_for_the_owner(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    monkeypatch.delenv("GITHUB_USER", raising=False)

    def _fail(timeout=15):
        raise AssertionError("gh must not run with --no-github")

    monkeypatch.setattr(orchestrator.github, "logged_in_login", _fail)
    orchestrator.orchestrate_create_app(
        slug="foo",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    assert mocks["render_template"].call_args.kwargs["data"]["github_user"] == ""


def test_platform_domain_has_no_built_in_value(monkeypatch, tmp_path):
    """No maintainer domain: preflight gets None, and the template gets the
    reserved placeholder."""
    mocks = _patch_all(monkeypatch, tmp_path)
    monkeypatch.delenv("PLATFORM_DOMAIN", raising=False)
    orchestrator.orchestrate_create_app(
        slug="foo",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    assert orchestrator.preflight.run_preflight.call_args.kwargs["platform_domain"] is None
    data = mocks["render_template"].call_args.kwargs["data"]
    assert data["platform_domain"] == orchestrator.PLACEHOLDER_PLATFORM_DOMAIN


def test_platform_domain_comes_from_the_arg_then_the_env(monkeypatch, tmp_path):
    mocks = _patch_all(monkeypatch, tmp_path)
    monkeypatch.setenv("PLATFORM_DOMAIN", "env.example")
    orchestrator.orchestrate_create_app(
        slug="foo",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        scaffolded_by="manual",
    )
    assert mocks["render_template"].call_args.kwargs["data"]["platform_domain"] == "env.example"

    orchestrator.orchestrate_create_app(
        slug="foo",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out2",
        scaffolded_by="manual",
        platform_domain="arg.example",
    )
    assert mocks["render_template"].call_args.kwargs["data"]["platform_domain"] == "arg.example"
    assert orchestrator.preflight.run_preflight.call_args.kwargs["platform_domain"] == "arg.example"


def test_dry_run_of_a_logto_plan_without_a_domain_fails(monkeypatch, tmp_path):
    _patch_all(monkeypatch, tmp_path)
    monkeypatch.delenv("PLATFORM_DOMAIN", raising=False)
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        dry_run=True,
        deploy_target="railway",
        auth_provider="logto",
        output_dir=tmp_path / "out",
        raise_on_failure=False,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "PREFLIGHT_ENV_MISSING"


def test_resume_with_a_different_domain_is_refused(monkeypatch, tmp_path):
    """The recorded API resource belongs to the first run's domain."""
    mocks = _patch_all(monkeypatch, tmp_path)
    state_path = tmp_path / ".bootstrap-state.json"
    from agentic_core.bootstrap import state as state_mod

    pre_state = BootstrapState(
        app_slug="foo",
        variant="full",
        scaffolded_by="manual",
        created_at=datetime.now(tz=UTC),
        checkpoints={"keystore": True, "logto": True},
        resource_ids={"logto_api_resource": "https://foo.first.example/api"},
        last_completed_step="logto",
    )
    state_mod.write_state(state_path, pre_state)
    before = state_path.read_bytes()

    result = orchestrator.orchestrate_create_app(
        slug="foo",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        resume=True,
        platform_domain="second.example",
        raise_on_failure=False,
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "PLATFORM_DOMAIN_MISMATCH"
    assert state_path.read_bytes() == before
    mocks["logto"].assert_not_called()


def test_resume_without_a_domain_reuses_the_recorded_one(monkeypatch, tmp_path):
    """A domain given only as a flag on the first run need not be typed again."""
    mocks = _patch_all(monkeypatch, tmp_path)
    monkeypatch.delenv("PLATFORM_DOMAIN", raising=False)
    state_path = tmp_path / ".bootstrap-state.json"
    from agentic_core.bootstrap import state as state_mod

    state_mod.write_state(
        state_path,
        BootstrapState(
            app_slug="foo",
            variant="full",
            scaffolded_by="manual",
            created_at=datetime.now(tz=UTC),
            checkpoints={"keystore": True, "logto": True},
            resource_ids={"logto_api_resource": "https://foo.first.example/api"},
            last_completed_step="logto",
        ),
    )
    orchestrator.orchestrate_create_app(
        slug="foo",
        with_railway=False,
        with_github=False,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        resume=True,
        raise_on_failure=False,
    )
    assert orchestrator.preflight.run_preflight.call_args.kwargs["platform_domain"] == (
        "first.example"
    )
    mocks["logto"].assert_not_called()
