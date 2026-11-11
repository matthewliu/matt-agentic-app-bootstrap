"""End-to-end integration tests for the bootstrap orchestrator (U9 / SC1, SC3).

These are the full-pipeline tests the reliability feature never had. They run
entirely offline: every external provider (preflight, keystore, Logto, GitHub,
Railway, verify) is stubbed via the ``_patch_all`` monkeypatch harness reused
from :mod:`tests.unit.test_bootstrap_orchestrator`. No real
Logto/GitHub/Railway/Postgres call is ever made.

Coverage:

- **SC1** — a clean full run drives every step once, ``verify()`` passes, and the
  state file is deleted at the end. Each provider *create* is called exactly once
  (no double-creates on the happy path).
- **SC3** — ``verify()`` is the final gate: it runs *after* the deploy step is
  marked done, receives the base URL derived from the persisted prod domain, and
  only on its success does the run reach state deletion.
"""

from __future__ import annotations

from unittest import mock

from agentic_core.bootstrap import orchestrator
from tests.unit.test_bootstrap_orchestrator import _patch_all


# ---------------------------------------------------------------------------
# SC1 — clean full run: every step once, verify passes, state deleted
# ---------------------------------------------------------------------------


def test_clean_full_run_calls_each_provider_once_and_deletes_state(monkeypatch, tmp_path):
    """SC1: a clean full run (auth + github + railway + postgres) drives every
    step exactly once and deletes the state file on success — no manual state
    edits, no double-creates."""
    mocks = _patch_all(monkeypatch, tmp_path)
    state_path = tmp_path / ".bootstrap-state.json"

    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="a full end-to-end app",
        variant="full",
        with_auth=True,
        with_db=True,
        with_railway=True,
        with_logto=True,
        with_github=True,
        railway_workspace="agent-sandbox",
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="agent",
    )

    assert result["status"] == "success"

    # Preflight gated the run once, up front.
    mocks["preflight"].assert_called_once()
    # Keystore decrypted exactly once (PERF-003).
    assert mocks["decrypt"].call_count == 1

    # Every provider *create* fired exactly once — no double-creates.
    assert mocks["logto"].call_count == 1
    assert mocks["github"].call_count == 1
    assert mocks["github_secret"].call_count == 1
    assert mocks["railway"].call_count == 1  # create_railway_project (init)
    assert mocks["railway_linked"].call_count == 1  # the check() before init
    assert mocks["railway_has_db"].call_count == 1
    assert mocks["railway_add_db"].call_count == 1  # add_database (postgres)
    assert mocks["railway_prod_domain"].call_count == 1
    assert mocks["logto_redirect"].call_count == 1
    assert mocks["render_template"].call_count == 1
    assert mocks["railway_deploy"].call_count == 1
    assert mocks["verify"].call_count == 1

    # Every hardened step is recorded as completed.
    for step in (
        "keystore",
        "logto",
        "github",
        "github_secrets",
        "railway",
        "railway_env",
        "postgres",
        "wiring",
        "template",
        "git_commit",
        "railway_deploy",
        "verify",
    ):
        assert step in result["completed_steps"], f"{step} missing from completed_steps"

    # Success path deletes the state file — no leftover partial state.
    assert not state_path.exists()


def test_clean_full_run_no_state_file_written_after_success(monkeypatch, tmp_path):
    """SC1: after a clean full run the .bootstrap-state.json is gone; a
    subsequent non-resume run would start fresh with no manual cleanup."""
    _patch_all(monkeypatch, tmp_path)
    state_path = tmp_path / ".bootstrap-state.json"
    orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
    )
    assert not state_path.exists()


# ---------------------------------------------------------------------------
# SC3 — verify() is the final gate: runs after deploy, gates state deletion
# ---------------------------------------------------------------------------


def test_verify_runs_after_deploy_and_is_the_final_success_gate(monkeypatch, tmp_path):
    """SC3: the mocked pipeline drives through to verify() success. verify must
    run AFTER the deploy step is marked done, and only its success reaches the
    state-delete / success envelope."""
    mocks = _patch_all(monkeypatch, tmp_path)
    state_path = tmp_path / ".bootstrap-state.json"

    # Attach deploy + verify to a manager so we can assert relative ordering.
    manager = mock.Mock()
    manager.attach_mock(mocks["railway_deploy"], "deploy")
    manager.attach_mock(mocks["verify"], "verify")

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

    # Ordering: deploy is invoked strictly before verify.
    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("deploy") < call_names.index("verify")

    # verify probes the URL derived from the persisted prod domain (STEP_WIRING).
    base_url = mocks["verify"].call_args.args[0]
    assert base_url == "https://foo-production.up.railway.app"

    # Final gate passed → state deleted.
    assert "verify" in result["completed_steps"]
    assert not state_path.exists()


def test_end_to_end_structural_verify_failure_leaves_state_and_is_non_retryable(
    monkeypatch, tmp_path
):
    """SC3/R8 boundary: if the deployed app is structurally broken (verify auth
    401), the pipeline does NOT declare success — it fails non-retryable and
    leaves the state file for inspection (never deletes it)."""
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
    assert model.status == "failed"
    assert model.checkpoint == "verify"
    assert model.retryable is False
    # State survives — the broken app must be inspectable, not silently cleaned.
    assert state_path.exists()


# ---------------------------------------------------------------------------
# U7 — Render target: preflight, template, validate, GitHub; nothing else
# ---------------------------------------------------------------------------


def test_clean_render_run_validates_before_github_and_deletes_state(monkeypatch, tmp_path):
    """A clean Render-target run validates the Blueprint before it creates the
    GitHub repo, calls no Railway, keystore, or Logto function, and deletes
    the state file on success."""
    mocks = _patch_all(
        monkeypatch,
        tmp_path,
        preflight_result={
            "status": "success",
            "checkpoint": "preflight",
            "render_workspace_id": "tea-abc123",
            "render_workspace_name": "My Workspace",
        },
    )
    state_path = tmp_path / ".bootstrap-state.json"
    manager = mock.Mock()
    manager.attach_mock(mocks["render_validate"], "validate")
    manager.attach_mock(mocks["github"], "github")

    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="a render app",
        deploy_target="render",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="agent",
    )

    assert result["status"] == "success"
    names = [c[0] for c in manager.mock_calls]
    assert names.index("validate") < names.index("github")
    for name in ("decrypt", "logto", "railway", "railway_deploy", "verify"):
        mocks[name].assert_not_called()
    assert result["render"]["status"] == "awaiting_blueprint_apply"
    assert result["auth"]["status"] == "not_configured"
    assert not state_path.exists()
