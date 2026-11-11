"""Unit tests for ``agentic_core.bootstrap.teardown`` — the U8 manual
escape hatch that deletes the per-app Logto + Railway resources a run
created.

The HARD INVARIANTS (KTD5) are the priority here and each has a dedicated
test:

  1. GitHub is NEVER deleted (no ``gh repo delete`` in the teardown path).
  2. The shared ``agentic-core-admin`` M2M app id (``LOGTO_M2M_APP_ID``) is
     NEVER a delete target, even if it appears in an app's ``resource_ids``.
  3. Teardown ALWAYS requires explicit confirmation unless ``assume_yes``.

Logto is exercised via a fake client (call-count + argument assertions);
Railway via a stubbed ``delete_project``. The real DELETE plumbing is
covered in ``test_bootstrap_logto.py`` / ``test_bootstrap_railway.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path

import pytest

from agentic_core.bootstrap import logto, railway, state as state_mod, teardown

ADMIN_ID = "agentic-core-admin-id"


@pytest.fixture(autouse=True)
def _logto_endpoint(monkeypatch):
    """Teardown refuses when the Logto endpoint is unknown (no built-in
    domain). Give every test a known endpoint; tests about endpoint
    resolution delete or override it themselves."""
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://logto.example.test")
    monkeypatch.delenv("PLATFORM_DOMAIN", raising=False)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class FakeLogtoClient:
    """Records delete calls; resolves finders from injected maps."""

    def __init__(self, *, resources=None, users=None, **_kwargs):
        self._resources = resources or {}
        self._users = users or {}
        self.deleted_apps: list[str] = []
        self.deleted_resources: list[str] = []
        self.deleted_users: list[str] = []

    def find_resource(self, indicator: str):
        return self._resources.get(indicator)

    def find_user(self, username: str):
        return self._users.get(username)

    def delete_application(self, app_id: str) -> bool:
        self.deleted_apps.append(app_id)
        return True

    def delete_api_resource(self, resource_id: str) -> bool:
        self.deleted_resources.append(resource_id)
        return True

    def delete_user(self, user_id: str) -> bool:
        self.deleted_users.append(user_id)
        return True


def _write_state(tmp_path: Path, slug: str, resource_ids: dict[str, str]) -> Path:
    app_dir = tmp_path / slug
    app_dir.mkdir(parents=True, exist_ok=True)
    state_path = app_dir / ".bootstrap-state.json"
    state_mod.write_state(
        state_path,
        state_mod.BootstrapState(
            app_slug=slug,
            variant="full",
            scaffolded_by="manual",
            created_at=datetime.now(UTC),
            resource_ids=resource_ids,
        ),
    )
    return state_path


def _full_resource_ids() -> dict[str, str]:
    return {
        "logto_spa_client_id": "spa-1",
        "logto_m2m_app_id": "m2m-1",
        "logto_api_resource": "https://foo.example.test/api",
        "logto_user_sub": "app_foo",
        "github_repo_url": "https://github.com/matt/foo",
        "railway_project_id": "proj-1",
    }


def _install_fakes(monkeypatch, *, fake=None, railway_calls=None):
    """Wire a FakeLogtoClient + a recording railway.delete_project."""
    fake = fake or FakeLogtoClient(
        resources={"https://foo.example.test/api": {"id": "res-1"}},
        users={"app_foo": {"id": "user-1"}},
    )
    monkeypatch.setattr(logto, "LogtoManagementClient", lambda **kw: fake)
    calls = railway_calls if railway_calls is not None else []

    def _stub_delete(**kwargs):
        calls.append(kwargs)
        return {"status": "success", "project": kwargs.get("project_id")}

    monkeypatch.setattr(railway, "delete_project", _stub_delete)
    return fake, calls


# ---------------------------------------------------------------------------
# Happy path: full state → every per-app resource deleted once
# ---------------------------------------------------------------------------


class TestFullTeardown:
    def test_deletes_each_logto_resource_and_railway_project(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        fake, railway_calls = _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )

        # SPA + M2M applications deleted (admin id is not among them here)
        assert set(fake.deleted_apps) == {"spa-1", "m2m-1"}
        # API resource resolved from indicator → internal id, then deleted
        assert fake.deleted_resources == ["res-1"]
        # user resolved from username → internal id, then deleted
        assert fake.deleted_users == ["user-1"]
        # railway project deleted exactly once with the persisted id
        assert len(railway_calls) == 1
        assert railway_calls[0].get("project_id") == "proj-1"
        assert summary["status"] == "success"

    def test_summary_is_success_envelope_shaped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )
        assert summary["app_slug"] == "foo"
        assert summary["variant"] == "full"
        assert summary["scaffolded_by"] == "manual"
        # validates cleanly through SuccessEnvelope (extra="allow")
        from agentic_core.output_schema import SuccessEnvelope

        SuccessEnvelope.model_validate(summary)


# ---------------------------------------------------------------------------
# INVARIANT 1 — GitHub is never deleted
# ---------------------------------------------------------------------------


class TestGitHubNeverDeleted:
    def test_github_repo_is_never_a_delete_target(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        fake, railway_calls = _install_fakes(monkeypatch)

        # A global subprocess spy: if teardown ever shelled out to `gh`, this
        # would capture it. Teardown must never run any gh command.
        import subprocess as _subprocess

        seen_argv: list[list[str]] = []
        real_run = _subprocess.run

        def _spy_run(argv, *a, **k):  # pragma: no cover - defensive
            seen_argv.append(list(argv) if isinstance(argv, (list, tuple)) else [argv])
            return real_run(argv, *a, **k)

        monkeypatch.setattr(_subprocess, "run", _spy_run)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )

        github_url = "https://github.com/matt/foo"
        # never handed to any delete sink
        assert github_url not in fake.deleted_apps
        assert github_url not in fake.deleted_resources
        assert github_url not in fake.deleted_users
        for call in railway_calls:
            assert github_url not in json.dumps(call)
        # no `gh ... delete` command was ever spawned
        for argv in seen_argv:
            assert not (argv and argv[0] == "gh" and "delete" in argv)
        # teardown reports it deliberately kept the repo
        assert "github_repo_url" in summary["teardown"]["preserved"]


# ---------------------------------------------------------------------------
# INVARIANT 2 — the shared admin app id is never a delete target
# ---------------------------------------------------------------------------


class TestAdminAppGuard:
    def test_admin_id_in_resource_ids_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        # Construct a hostile/erroneous state where the per-app m2m id
        # somehow equals the shared admin app id.
        rids = _full_resource_ids()
        rids["logto_m2m_app_id"] = ADMIN_ID
        state_path = _write_state(tmp_path, "foo", rids)
        fake, _ = _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )

        # The admin id must NEVER be deleted...
        assert ADMIN_ID not in fake.deleted_apps
        # ...but the genuine per-app SPA still is.
        assert "spa-1" in fake.deleted_apps
        assert ADMIN_ID in summary["teardown"]["skipped_admin"]

    def test_admin_id_via_explicit_param_is_skipped(self, tmp_path, monkeypatch):
        # No env var; admin id passed explicitly.
        monkeypatch.delenv("LOGTO_M2M_APP_ID", raising=False)
        rids = _full_resource_ids()
        rids["logto_spa_client_id"] = ADMIN_ID
        state_path = _write_state(tmp_path, "foo", rids)
        fake, _ = _install_fakes(monkeypatch)

        teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path, admin_app_id=ADMIN_ID
        )
        assert ADMIN_ID not in fake.deleted_apps


# ---------------------------------------------------------------------------
# INVARIANT 3 — always confirm unless assume_yes
# ---------------------------------------------------------------------------


class TestConfirmationRequired:
    def test_declined_confirm_deletes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        fake, railway_calls = _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo",
            assume_yes=False,
            confirm=lambda _msg: False,  # operator says no
            state_path=state_path,
        )

        assert fake.deleted_apps == []
        assert fake.deleted_resources == []
        assert fake.deleted_users == []
        assert railway_calls == []
        assert summary["teardown"]["aborted"] is True

    def test_no_confirm_and_not_assume_yes_deletes_nothing(self, tmp_path, monkeypatch):
        """Safe default: without a confirmation mechanism AND without
        assume_yes, teardown must delete NOTHING."""
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        fake, railway_calls = _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=False, state_path=state_path
        )
        assert fake.deleted_apps == []
        assert railway_calls == []
        assert summary["teardown"]["aborted"] is True

    def test_confirm_accepted_proceeds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        fake, railway_calls = _install_fakes(monkeypatch)

        teardown.teardown_app(
            "foo",
            assume_yes=False,
            confirm=lambda _msg: True,
            state_path=state_path,
        )
        assert "spa-1" in fake.deleted_apps
        assert len(railway_calls) == 1


# ---------------------------------------------------------------------------
# Partial state — delete what exists, report the rest as already-absent
# ---------------------------------------------------------------------------


class TestPartialState:
    def test_missing_ids_reported_absent_and_run_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        # Only SPA + railway present; M2M / api resource / user absent.
        rids = {
            "logto_spa_client_id": "spa-1",
            "railway_project_id": "proj-1",
        }
        state_path = _write_state(tmp_path, "foo", rids)
        fake, railway_calls = _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )

        assert fake.deleted_apps == ["spa-1"]
        assert fake.deleted_resources == []
        assert fake.deleted_users == []
        assert len(railway_calls) == 1
        assert summary["status"] == "success"
        absent = summary["teardown"]["already_absent"]
        assert "logto_m2m_app_id" in absent
        assert "logto_api_resource" in absent
        assert "logto_user_sub" in absent


# ---------------------------------------------------------------------------
# Fix 1 — admin guard must FAIL CLOSED (KTD5). When the admin app id cannot be
# resolved (LOGTO_M2M_APP_ID unset AND no admin_app_id passed) we cannot prove
# a Logto delete target isn't the shared admin app, so we must REFUSE the Logto
# deletes rather than treat "unknown admin id" as "nothing is admin".
# ---------------------------------------------------------------------------


class TestAdminGuardFailsClosed:
    def test_unresolved_admin_id_refuses_logto_deletes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOGTO_M2M_APP_ID", raising=False)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        fake, railway_calls = _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )

        # NO Logto delete of any kind happened — the guard is unresolved.
        assert fake.deleted_apps == []
        assert fake.deleted_resources == []
        assert fake.deleted_users == []
        # The whole destructive command is refused → Railway untouched too.
        assert railway_calls == []
        # Refusal is clearly signaled.
        td = summary["teardown"]
        assert td["aborted"] is True
        assert "admin" in (td.get("aborted_reason") or "").lower()

    def test_resolved_admin_id_still_allows_teardown(self, tmp_path, monkeypatch):
        """Sanity: with the admin id resolvable, teardown proceeds normally."""
        monkeypatch.delenv("LOGTO_M2M_APP_ID", raising=False)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        fake, railway_calls = _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path, admin_app_id=ADMIN_ID
        )
        assert "spa-1" in fake.deleted_apps
        assert len(railway_calls) == 1
        assert summary["teardown"]["aborted"] is False


# ---------------------------------------------------------------------------
# Fix 2 — slug validation. teardown is a DESTRUCTIVE command; an unvalidated
# slug is joined into a Path, so `../../evil` would escape the sibling-apps dir.
# ---------------------------------------------------------------------------


class TestSlugValidation:
    def test_traversal_slug_rejected_no_side_effects(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        fake, railway_calls = _install_fakes(monkeypatch)

        with pytest.raises(ValueError):
            teardown.teardown_app("../../evil", assume_yes=True)

        # No path resolved, no client constructed, nothing deleted.
        assert fake.deleted_apps == []
        assert fake.deleted_resources == []
        assert fake.deleted_users == []
        assert railway_calls == []

    def test_uppercase_slug_rejected(self, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        with pytest.raises(ValueError):
            teardown.teardown_app("BAD-Slug", assume_yes=True)


# ---------------------------------------------------------------------------
# Fix 4 — the `failed` branches (Logto delete raises, Railway returns failed).
# ---------------------------------------------------------------------------


class TestFailedBranch:
    def test_logto_delete_errors_land_in_failed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())

        class RaisingClient(FakeLogtoClient):
            def delete_application(self, app_id):
                raise logto.LogtoError("boom-app")

            def delete_api_resource(self, resource_id):
                raise logto.LogtoError("boom-resource")

            def delete_user(self, user_id):
                raise logto.LogtoError("boom-user")

        fake = RaisingClient(
            resources={"https://foo.example.test/api": {"id": "res-1"}},
            users={"app_foo": {"id": "user-1"}},
        )
        _install_fakes(monkeypatch, fake=fake)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )
        failed = summary["teardown"]["failed"]
        deleted = summary["teardown"]["deleted"]
        for key in (
            "logto_spa_client_id",
            "logto_m2m_app_id",
            "logto_api_resource",
            "logto_user_sub",
        ):
            assert key in failed, f"{key} should be in failed"
            assert key not in deleted, f"{key} must NOT be in deleted"
        # Finding 2b: a non-empty `failed` list must flip the envelope status.
        assert summary["status"] == "failed"

    def test_railway_failed_status_lands_in_failed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        _install_fakes(monkeypatch)

        def _fail_delete(**kwargs):
            return {"status": "failed", "error": "railway said no"}

        monkeypatch.setattr(railway, "delete_project", _fail_delete)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )
        assert "railway_project_id" in summary["teardown"]["failed"]
        assert "railway_project_id" not in summary["teardown"]["deleted"]
        # Finding 2b: a failed railway delete makes the whole teardown a failure.
        assert summary["status"] == "failed"


# ---------------------------------------------------------------------------
# Finding 2b — the envelope `status` must reflect the actual outcome. A
# hardcoded "success" masks failed deletes and guard refusals from callers.
# ---------------------------------------------------------------------------


class TestSummaryStatus:
    def test_admin_guard_refusal_status_is_failed(self, tmp_path, monkeypatch):
        # Unresolved admin id → refusal (aborted_reason set). A refusal is not a
        # success: nothing was torn down and the operator must act.
        monkeypatch.delenv("LOGTO_M2M_APP_ID", raising=False)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )
        assert summary["teardown"]["aborted_reason"]
        assert summary["status"] == "failed"

    def test_fully_successful_teardown_status_is_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )
        assert summary["teardown"]["failed"] == []
        assert summary["teardown"]["aborted_reason"] is None
        assert summary["status"] == "success"

    def test_declined_confirmation_is_not_a_failure(self, tmp_path, monkeypatch):
        # A user who declines the prompt chose to stop — nothing FAILED, so this
        # stays "success" (aborted=True, aborted_reason=None).
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo",
            assume_yes=False,
            confirm=lambda _msg: False,
            state_path=state_path,
        )
        assert summary["teardown"]["aborted"] is True
        assert summary["teardown"]["aborted_reason"] is None
        assert summary["status"] == "success"


# ---------------------------------------------------------------------------
# Finding 2a — teardown must resolve the Logto endpoint from LOGTO_ENDPOINT
# (matching preflight/orchestrator) BEFORE the constructed platform default.
# There is no built-in domain, so with no endpoint and no domain teardown
# refuses before any delete.
# ---------------------------------------------------------------------------


class TestLogtoEndpointResolution:
    def _capture_endpoint(self, monkeypatch):
        captured: dict[str, object] = {}
        fake = FakeLogtoClient(
            resources={"https://foo.example.test/api": {"id": "res-1"}},
            users={"app_foo": {"id": "user-1"}},
        )

        def _factory(**kw):
            captured.update(kw)
            return fake

        monkeypatch.setattr(logto, "LogtoManagementClient", _factory)
        monkeypatch.setattr(
            railway, "delete_project", lambda **kw: {"status": "success"}
        )
        return captured

    def test_endpoint_falls_back_to_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        monkeypatch.setenv("LOGTO_ENDPOINT", "https://logto.example.dev")
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        captured = self._capture_endpoint(monkeypatch)

        teardown.teardown_app("foo", assume_yes=True, state_path=state_path)
        assert captured["endpoint"] == "https://logto.example.dev"

    def test_explicit_endpoint_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        monkeypatch.setenv("LOGTO_ENDPOINT", "https://env.example.dev")
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        captured = self._capture_endpoint(monkeypatch)

        teardown.teardown_app(
            "foo",
            assume_yes=True,
            state_path=state_path,
            logto_endpoint="https://explicit.example.dev",
        )
        assert captured["endpoint"] == "https://explicit.example.dev"

    def test_constructed_default_when_neither_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        monkeypatch.delenv("LOGTO_ENDPOINT", raising=False)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        captured = self._capture_endpoint(monkeypatch)

        teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path, platform_domain="demo"
        )
        assert captured["endpoint"] == "https://auth.demo"

    def test_refuses_when_endpoint_and_domain_are_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        monkeypatch.delenv("LOGTO_ENDPOINT", raising=False)
        monkeypatch.delenv("PLATFORM_DOMAIN", raising=False)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        railway_calls: list = []
        fake, _ = _install_fakes(monkeypatch, railway_calls=railway_calls)

        summary = teardown.teardown_app("foo", assume_yes=True, state_path=state_path)

        assert summary["status"] == "failed"
        td = summary["teardown"]
        assert td["aborted"] is True
        assert td["deleted"] == []
        assert td["aborted_reason"].startswith("logto_endpoint_unresolved")
        assert railway_calls == []


# ---------------------------------------------------------------------------
# Fix 6 — the loaded state must belong to the requested slug. A stale/copied/
# wrong state file must NOT cause `teardown app-a` to delete app-b's resources.
# ---------------------------------------------------------------------------


class TestSlugStateBinding:
    def test_state_slug_mismatch_refuses_zero_deletes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        # Write a state file whose recorded app_slug is a DIFFERENT app, then
        # resolve it via an explicit state_path for slug "foo".
        app_dir = tmp_path / "foo"
        app_dir.mkdir(parents=True, exist_ok=True)
        state_path = app_dir / ".bootstrap-state.json"
        state_mod.write_state(
            state_path,
            state_mod.BootstrapState(
                app_slug="a-different-app",  # mismatch!
                variant="full",
                scaffolded_by="manual",
                created_at=datetime.now(UTC),
                resource_ids=_full_resource_ids(),
            ),
        )
        fake, railway_calls = _install_fakes(monkeypatch)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )

        # Refused BEFORE any delete.
        assert fake.deleted_apps == []
        assert fake.deleted_resources == []
        assert fake.deleted_users == []
        assert railway_calls == []
        td = summary["teardown"]
        assert td["aborted"] is True
        assert "mismatch" in (td.get("aborted_reason") or "").lower()
        assert summary["status"] == "failed"
        # The mismatched state file is NOT cleared (nothing was torn down).
        assert state_path.exists()


# ---------------------------------------------------------------------------
# Fix 7 — a successful teardown clears the state file so a later --resume does
# not chase deleted resources; a refused/aborted/failed teardown leaves it.
# ---------------------------------------------------------------------------


class TestStateFileCleanup:
    def test_successful_teardown_removes_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        _install_fakes(monkeypatch)
        assert state_path.exists()

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )
        assert summary["status"] == "success"
        assert not state_path.exists()

    def test_declined_confirmation_leaves_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        _install_fakes(monkeypatch)

        teardown.teardown_app(
            "foo",
            assume_yes=False,
            confirm=lambda _msg: False,
            state_path=state_path,
        )
        assert state_path.exists()

    def test_failed_teardown_leaves_state_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        state_path = _write_state(tmp_path, "foo", _full_resource_ids())
        _install_fakes(monkeypatch)

        def _fail_delete(**kwargs):
            return {"status": "failed", "error": "railway said no"}

        monkeypatch.setattr(railway, "delete_project", _fail_delete)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )
        assert summary["status"] == "failed"
        # A failed teardown must NOT clear state — resume/retry still needs it.
        assert state_path.exists()


# ---------------------------------------------------------------------------
# Fix 5 — already_absent path for state-recorded-but-Logto-absent indicators,
# and the admin guard on the RESOLVED internal id (not just the stored key).
# ---------------------------------------------------------------------------


class TestAbsentAndResolvedAdminGuard:
    def test_indicator_and_username_absent_in_logto(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        rids = {
            "logto_api_resource": "https://foo.example.test/api",
            "logto_user_sub": "app_foo",
        }
        state_path = _write_state(tmp_path, "foo", rids)
        # Finders return None → the resource/user is already gone in Logto.
        fake = FakeLogtoClient(resources={}, users={})
        _install_fakes(monkeypatch, fake=fake)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )
        absent = summary["teardown"]["already_absent"]
        assert "logto_api_resource" in absent
        assert "logto_user_sub" in absent
        assert fake.deleted_resources == []
        assert fake.deleted_users == []

    def test_resolved_internal_id_matching_admin_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", ADMIN_ID)
        rids = {
            "logto_api_resource": "https://foo.example.test/api",
            "logto_user_sub": "app_foo",
        }
        state_path = _write_state(tmp_path, "foo", rids)
        # The indicator/username differ from ADMIN_ID, but they RESOLVE to it.
        fake = FakeLogtoClient(
            resources={"https://foo.example.test/api": {"id": ADMIN_ID}},
            users={"app_foo": {"id": ADMIN_ID}},
        )
        _install_fakes(monkeypatch, fake=fake)

        summary = teardown.teardown_app(
            "foo", assume_yes=True, state_path=state_path
        )
        assert ADMIN_ID in summary["teardown"]["skipped_admin"]
        # Guard fired on the resolved id → neither delete was issued.
        assert fake.deleted_resources == []
        assert fake.deleted_users == []
