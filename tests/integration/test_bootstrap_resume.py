"""Resume / idempotency integration tests for the bootstrap orchestrator.

U9 / SC2, SC4, R8. These prove the recovery contract the reliability feature
never had test coverage for: a mid-pipeline kill resumes cleanly with no
double-creates or orphaned resources, a bad-credential run is blocked before any
external resource is created, and a *structural* (non-retryable) failure causes
``--resume`` to refuse rather than loop.

All external providers are stubbed via the ``_patch_all`` harness reused from
:mod:`tests.unit.test_bootstrap_orchestrator`, so these run fully offline. A
mid-pipeline kill is simulated by making a chosen stubbed step raise / return a
failure envelope, then re-running ``orchestrate_create_app(resume=True, ...)``
against the SAME state file and asserting the recovery contract.
"""

from __future__ import annotations

from datetime import datetime, UTC

import pytest

from agentic_core.bootstrap import orchestrator
from agentic_core.bootstrap import state as state_mod
from agentic_core.bootstrap.state import BootstrapState
from tests.unit.test_bootstrap_orchestrator import _RW_UUID, _patch_all


# ---------------------------------------------------------------------------
# SC2 — kill mid-Logto (after SPA persist), resume adopts, no duplicate SPA
# ---------------------------------------------------------------------------


def test_kill_after_logto_spa_create_resume_adopts_and_completes(monkeypatch, tmp_path):
    """SC2: a run killed mid-Logto (SPA + M2M IDs persisted, then the API
    resource create raises) leaves recoverable — not orphaned — state. On
    ``--resume`` the SPA/M2M IDs survive and provision_app is re-invoked exactly
    once (its internal finders adopt the existing resources); the run completes
    and the state file is deleted. No duplicate SPA is created."""
    state_path = tmp_path / ".bootstrap-state.json"

    # --- Run 1: kill mid-Logto after the SPA + M2M IDs are persisted. ---
    mocks1 = _patch_all(monkeypatch, tmp_path)

    def _provision_then_die(*, persist=None, **kwargs):
        assert persist is not None, "orchestrator must thread a persist callback"
        persist("logto_spa_client_id", "spa-live")
        persist("logto_m2m_app_id", "m2m-live")
        raise RuntimeError("api resource create failed mid-sequence")

    mocks1["logto"].side_effect = _provision_then_die

    with pytest.raises(SystemExit):
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

    # The IDs created before the kill are recoverable (persisted), and STEP_LOGTO
    # is NOT marked done — so resume will re-enter Logto and adopt them.
    raw_after_kill = state_path.read_text()
    assert "logto_spa_client_id" in raw_after_kill and "spa-live" in raw_after_kill
    assert "logto_m2m_app_id" in raw_after_kill and "m2m-live" in raw_after_kill
    assert '"logto":true' not in raw_after_kill and '"logto": true' not in raw_after_kill

    # --- Run 2: resume against the SAME state file; Logto now succeeds. ---
    mocks2 = _patch_all(monkeypatch, tmp_path)  # fresh mocks (call counts reset)

    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        resume=True,
    )

    assert result["status"] == "success"
    # provision_app re-invoked exactly ONCE on resume (adopt path) — not twice,
    # no orphan/duplicate SPA.
    assert mocks2["logto"].call_count == 1
    # GitHub was never completed in run 1, so it runs once on resume — but only
    # once (no double-create).
    assert mocks2["github"].call_count == 1
    # Clean completion deletes the state file.
    assert not state_path.exists()


# ---------------------------------------------------------------------------
# SC2 / R3 — kill after Railway init, resume does not re-init (no double create)
# ---------------------------------------------------------------------------


def test_kill_after_railway_init_resume_does_not_reinit(monkeypatch, tmp_path):
    """SC2/R3: a genuine mid-pipeline kill AFTER Railway init (STEP_RAILWAY
    marked done) followed by ``--resume`` must NOT re-run ``railway init`` —
    ``create_railway_project`` is called zero times on the resume run."""
    state_path = tmp_path / ".bootstrap-state.json"

    # --- Run 1: init succeeds, then the Postgres add fails (transient). ---
    mocks1 = _patch_all(
        monkeypatch,
        tmp_path,
        railway_result={
            "status": "success",
            "name": "foo",
            "workspace": None,
            "project_id": _RW_UUID,
        },
        railway_add_db_result={
            "status": "failed",
            "error_code": "RAILWAY_CLI_ERROR",
            "exit_code": 1,
            "error_message": "add boom",
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
            state_path=state_path,
            scaffolded_by="manual",
        )
    # init ran once in run 1; STEP_RAILWAY is marked, STEP_POSTGRES is not.
    assert mocks1["railway"].call_count == 1
    raw = state_path.read_text()
    assert '"railway":true' in raw or '"railway": true' in raw
    assert '"postgres":true' not in raw and '"postgres": true' not in raw

    # --- Run 2: resume; Postgres add now succeeds. ---
    mocks2 = _patch_all(monkeypatch, tmp_path)  # add_database succeeds by default
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
    # STEP_RAILWAY was already done → the whole block is skipped: neither the
    # check() (get_linked_project) nor init (create_railway_project) is called.
    assert mocks2["railway"].call_count == 0
    assert mocks2["railway_linked"].call_count == 0
    # Postgres, the step that failed, is retried and completes once.
    assert mocks2["railway_add_db"].call_count == 1
    assert not state_path.exists()


def test_resume_adopts_linked_project_when_checkpoint_not_marked(monkeypatch, tmp_path):
    """SC2/R3 (crash-between-init-and-mark window): if a prior run created the
    Railway project but crashed before STEP_RAILWAY was marked, resume sees the
    project via ``get_linked_project`` and adopts it — ``railway init`` is NOT
    re-run (init call count 0)."""
    state_path = tmp_path / ".bootstrap-state.json"
    # Pre-seed a state file where Logto is done but Railway is NOT marked, to
    # model the crash-between-init-and-checkpoint window. `railway_env` is marked
    # done so this resume does not trip Fix 1's LOGTO_SECRET_UNRECOVERABLE guard
    # (the M2M secret is gone once STEP_LOGTO is checkpointed, so the secret-
    # dependent steps must already be complete for a resume to proceed); this
    # test isolates the linked-project *adoption* behaviour, not the secret path.
    pre_state = BootstrapState(
        app_slug="foo",
        variant="full",
        scaffolded_by="manual",
        created_at=datetime.now(tz=UTC),
        checkpoints={"keystore": True, "logto": True, "railway_env": True},
        resource_ids={
            "logto_spa_client_id": "spa-abc",
            "logto_m2m_app_id": "m2m-abc",
        },
        last_completed_step="logto",
    )
    state_mod.write_state(state_path, pre_state)

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
    # check() saw the linked project → init MUST NOT be called.
    mocks["railway_linked"].assert_called_once()
    assert mocks["railway"].call_count == 0
    # Logto was already done → provision_app not re-run (no duplicate SPA).
    mocks["logto"].assert_not_called()
    mocks["decrypt"].assert_not_called()
    assert not state_path.exists()


# ---------------------------------------------------------------------------
# Fix 1 (P0) — resume past STEP_LOGTO with secret steps NOT done must FAIL LOUD
# ---------------------------------------------------------------------------


def test_resume_past_logto_without_secret_steps_fails_unrecoverable(
    monkeypatch, tmp_path
):
    """Fix 1: on ``--resume`` where STEP_LOGTO is already checkpointed but the
    secret-dependent steps (github_secrets / railway_env) are NOT, the M2M app
    secret is gone — it only lived in memory during the original STEP_LOGTO run
    and is never persisted (R8 entropy gate). The orchestrator MUST NOT silently
    skip those steps and proceed to deploy/verify (which would ship an app whose
    GitHub Actions secret + Railway env vars were never pushed). It must fail
    loud, non-retryable, with a LOGTO_SECRET_UNRECOVERABLE-style code, and NOT
    deploy or verify."""
    from agentic_core.output_schema import FailureEnvelope

    state_path = tmp_path / ".bootstrap-state.json"
    # STEP_LOGTO done (secret already gone), secret steps NOT done.
    pre_state = BootstrapState(
        app_slug="foo",
        variant="full",
        scaffolded_by="manual",
        created_at=datetime.now(tz=UTC),
        checkpoints={"keystore": True, "logto": True},
        resource_ids={
            "logto_spa_client_id": "spa-abc",
            "logto_m2m_app_id": "m2m-abc",
        },
        last_completed_step="logto",
    )
    state_mod.write_state(state_path, pre_state)

    mocks = _patch_all(monkeypatch, tmp_path)
    envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        with_railway=True,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        resume=True,
        raise_on_failure=False,
    )
    # Did NOT return success — failed loud, non-retryable.
    model = FailureEnvelope.model_validate(envelope)
    assert model.status == "failed"
    assert model.error_code == "LOGTO_SECRET_UNRECOVERABLE"
    assert model.retryable is False
    # The failure fired at the first secret step (github_secrets runs before
    # railway_env), before any deploy/verify.
    assert model.checkpoint == "github_secrets"
    mocks["railway_deploy"].assert_not_called()
    mocks["verify"].assert_not_called()
    # Logto/keystore were NOT re-run (already done), and the secret was never
    # pushed (the whole point — it's unrecoverable).
    mocks["logto"].assert_not_called()
    mocks["decrypt"].assert_not_called()
    mocks["github_secret"].assert_not_called()
    mocks["railway_env"].assert_not_called()


def test_resume_past_logto_railway_env_only_fails_unrecoverable(
    monkeypatch, tmp_path
):
    """Fix 1 (Railway-env variant): with_github=False so github_secrets is
    skipped entirely, but with_railway=True still needs the M2M secret for
    STEP_RAILWAY_ENV. Resuming past STEP_LOGTO with railway_env not done must
    fail loud non-retryable rather than deploy an app missing its env vars."""
    from agentic_core.output_schema import FailureEnvelope

    state_path = tmp_path / ".bootstrap-state.json"
    pre_state = BootstrapState(
        app_slug="foo",
        variant="full",
        scaffolded_by="manual",
        created_at=datetime.now(tz=UTC),
        checkpoints={"keystore": True, "logto": True, "railway": True},
        resource_ids={
            "logto_spa_client_id": "spa-abc",
            "railway_project_id": _RW_UUID,
        },
        last_completed_step="railway",
    )
    state_mod.write_state(state_path, pre_state)

    mocks = _patch_all(monkeypatch, tmp_path)
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
    assert model.status == "failed"
    assert model.error_code == "LOGTO_SECRET_UNRECOVERABLE"
    assert model.retryable is False
    assert model.checkpoint == "railway_env"
    mocks["railway_env"].assert_not_called()
    mocks["railway_deploy"].assert_not_called()
    mocks["verify"].assert_not_called()


# ---------------------------------------------------------------------------
# Finding 5 — SPA-only variant (static-dashboard --with-auth) has NO M2M secret,
# so a resume past STEP_LOGTO must NOT wedge on LOGTO_SECRET_UNRECOVERABLE.
# ---------------------------------------------------------------------------


def _env_names(env_mock):
    """Extract the ``name=`` of every set_env_var call (kwarg or positional)."""
    names = []
    for c in env_mock.call_args_list:
        names.append(c.kwargs.get("name") or (c.args[0] if c.args else None))
    return names


def test_static_dashboard_resume_past_logto_completes_without_secret(
    monkeypatch, tmp_path
):
    """Finding 5: ``static-dashboard --with-auth`` is a REAL variant with NO M2M
    secret (with_m2m=False). Resuming past a checkpointed STEP_LOGTO (logto_result
    is None) must NOT fail with LOGTO_SECRET_UNRECOVERABLE — there was never a
    secret to recover. STEP_GITHUB_SECRETS no-ops; STEP_RAILWAY_ENV pushes the
    NON-secret vars sourced from persisted state (SPA_CLIENT_ID, USER_SUB,
    LOGTO_ENDPOINT) but NOT the M2M secret; the run completes."""
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
    state_mod.write_state(state_path, pre_state)

    mocks = _patch_all(monkeypatch, tmp_path)
    result = orchestrator.orchestrate_create_app(
        slug="foo",
        description="x",
        variant="static-dashboard",
        with_auth=True,
        with_railway=True,
        with_github=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=state_path,
        scaffolded_by="manual",
        resume=True,
    )
    assert result["status"] == "success"
    # STEP_GITHUB_SECRETS no-ops (no M2M secret exists for this variant).
    mocks["github_secret"].assert_not_called()
    # STEP_RAILWAY_ENV pushed the non-secret vars sourced from state...
    names = _env_names(mocks["railway_env"])
    assert "FOO_SPA_CLIENT_ID" in names
    assert "FOO_USER_SUB" in names
    assert "FOO_LOGTO_ENDPOINT" in names
    # ...but NEVER the M2M secret (there is none).
    assert "FOO_M2M_APP_SECRET" not in names
    # Clean completion deletes the state file.
    assert not state_path.exists()


# ---------------------------------------------------------------------------
# SC4 — preflight blocks bad creds before any external resource is created
# ---------------------------------------------------------------------------


def test_preflight_blocks_bad_creds_before_any_create(monkeypatch, tmp_path):
    """SC4: a failing preflight aborts the run before ANY provider create /
    keystore decrypt happens, and the envelope is non-retryable (nothing was
    created, so there is nothing to resume)."""
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
        with_db=True,
        keystore_path=tmp_path / "admin.keystore.json",
        output_dir=tmp_path / "out",
        state_path=tmp_path / ".bootstrap-state.json",
        scaffolded_by="manual",
        raise_on_failure=False,
    )
    mocks["preflight"].assert_called_once()
    # NOTHING downstream fired — no keystore, no provider create of any kind.
    mocks["decrypt"].assert_not_called()
    mocks["load_keystore"].assert_not_called()
    mocks["logto"].assert_not_called()
    mocks["github"].assert_not_called()
    mocks["github_secret"].assert_not_called()
    mocks["railway"].assert_not_called()
    mocks["railway_linked"].assert_not_called()
    mocks["railway_env"].assert_not_called()
    mocks["railway_has_db"].assert_not_called()
    mocks["railway_add_db"].assert_not_called()
    mocks["railway_prod_domain"].assert_not_called()
    mocks["railway_deploy"].assert_not_called()
    mocks["verify"].assert_not_called()
    mocks["render_template"].assert_not_called()

    model = FailureEnvelope.model_validate(envelope)
    assert model.error_code == "PREFLIGHT_RAILWAY_MISSING"
    assert model.retryable is False


# ---------------------------------------------------------------------------
# R8 — a non-retryable (structural) failure causes --resume to REFUSE
# ---------------------------------------------------------------------------


def test_non_retryable_verify_failure_refuses_resume(monkeypatch, tmp_path):
    """R8: a STRUCTURAL verify failure (auth 401) persists a non-retryable
    marker; on ``--resume`` the orchestrator REFUSES to re-run — it re-executes
    no steps and reports the structural failure — instead of looping on a
    genuinely broken app."""
    from agentic_core.output_schema import FailureEnvelope

    state_path = tmp_path / ".bootstrap-state.json"

    # --- Run 1: deploy succeeds, verify structurally fails (auth 401). ---
    _patch_all(
        monkeypatch,
        tmp_path,
        verify_result={
            "status": "structural",
            "error_code": "VERIFY_AUTH_REJECTED",
            "message": "deployed app rejected a valid app token (401)",
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
            state_path=state_path,
            scaffolded_by="manual",
        )
    # The non-retryable marker is persisted so resume can honor it.
    persisted = state_mod.read_state(state_path)
    assert persisted.last_failure_retryable is False
    assert persisted.last_failure_code == "VERIFY_AUTH_REJECTED"

    # --- Run 2: resume. Even though verify would now succeed, resume must
    # refuse to re-run a structurally-broken app. ---
    mocks2 = _patch_all(monkeypatch, tmp_path)  # verify would PASS on this run
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
    # It refused: NO steps were re-executed.
    mocks2["decrypt"].assert_not_called()
    mocks2["logto"].assert_not_called()
    mocks2["railway"].assert_not_called()
    mocks2["railway_deploy"].assert_not_called()
    mocks2["verify"].assert_not_called()
    mocks2["render_template"].assert_not_called()

    # And it reported the structural failure, non-retryable.
    model = FailureEnvelope.model_validate(envelope)
    assert model.status == "failed"
    assert model.checkpoint == "resume"
    assert model.error_code == "VERIFY_AUTH_REJECTED"
    assert model.retryable is False


def test_retryable_failure_still_allows_resume(monkeypatch, tmp_path):
    """R8 boundary: a TRANSIENT (retryable) failure must NOT block resume — the
    marker stays truthy and ``--resume`` proceeds to completion. This guards the
    refuse logic against over-triggering on ordinary transient failures."""
    state_path = tmp_path / ".bootstrap-state.json"

    # --- Run 1: deploy fails transiently (retryable). ---
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
    persisted = state_mod.read_state(state_path)
    # Transient failure → marker is retryable-True, resume is allowed.
    assert persisted.last_failure_retryable is True

    # --- Run 2: resume; deploy now succeeds → run completes. ---
    mocks2 = _patch_all(monkeypatch, tmp_path)
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
    # The deploy step (which failed) is retried and completes.
    mocks2["railway_deploy"].assert_called_once()
    # verify ran as the final gate after the successful redeploy.
    mocks2["verify"].assert_called_once()
    assert not state_path.exists()
