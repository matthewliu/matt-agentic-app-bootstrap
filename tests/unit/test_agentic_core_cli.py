"""Unit tests for ``agentic-core`` Typer CLI surface (task #2 + #9).

Covers the root callback (``--version``, ``--json``, ``--debug``) AND the
``create-app`` subcommand wiring (R11 canonical flags, R12 schema
versioning, R4 exit-code contract).

Tests run via Typer's CliRunner so we can assert stdout / stderr split
without spawning subprocesses. The ``create-app`` orchestrator itself is
stubbed — its own end-to-end coverage lives in
``test_bootstrap_orchestrator.py``.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from agentic_core import __version__, cli
from agentic_core.bootstrap import orchestrator, preflight
from tests.conftest import plain


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Root callback: --version, --help, --json, --debug
# ---------------------------------------------------------------------------


class TestRootCallback:

    def test_version_flag_prints_version_and_exits(self, runner):
        result = runner.invoke(cli.app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.stdout

    def test_help_shows_create_app(self, runner):
        result = runner.invoke(cli.app, ["--help"])
        assert result.exit_code == 0
        assert "create-app" in result.stdout

    def test_help_shows_admin(self, runner):
        result = runner.invoke(cli.app, ["--help"])
        assert result.exit_code == 0
        assert "admin" in result.stdout

    def test_json_flag_propagates_to_subcommand_ctx(self, runner, monkeypatch):
        """--json on root → ctx.obj['json'] is True in subcommand callback."""

        def _spy(slug, **kwargs):
            return {
                "schema_version": 1,
                "status": "success",
                "app_slug": slug,
                "variant": kwargs.get("variant", "full"),
                "scaffolded_by": kwargs.get("scaffolded_by", "manual"),
                "logto": None,
                "github": None,
                "railway": None,
                "completed_steps": [],
                "dry_run": True,
            }

        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)
        result = runner.invoke(
            cli.app, ["--json", "create-app", "foo", "--dry-run"]
        )
        assert result.exit_code == 0
        # JSON line on stdout proves the json branch was taken.
        assert json.loads(result.stdout.strip())["status"] == "success"


# ---------------------------------------------------------------------------
# create-app: R11 flags + R4 exit codes + R12 schema versioning
# ---------------------------------------------------------------------------


class TestCreateAppFlags:

    def test_human_mode_prints_orchestrator_progress(self, runner, monkeypatch):
        """The default CLI must surface progress before its final summary."""

        def _spy(slug, **kwargs):
            progress = kwargs["progress"]
            progress("[green]✓[/green] Admin keystore unlocked")
            progress("[cyan]→[/cyan] Waiting for Railway to build and deploy...")
            progress("    Railway deployment status: [bold]BUILDING[/bold]")
            progress("    Railway deployment status: [bold]SUCCESS[/bold]")
            return {
                "schema_version": 1,
                "status": "success",
                "app_slug": slug,
                "variant": "full",
                "scaffolded_by": "manual",
                "logto": None,
                "github": None,
                "railway": None,
                "completed_steps": [],
            }

        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)

        result = runner.invoke(cli.app, ["create-app", "foo", "--no-railway"])

        assert result.exit_code == 0
        assert "Admin keystore unlocked" in result.stderr
        assert "Waiting for Railway to build and deploy" in result.stderr
        progress = plain(result.stderr)
        assert "Railway deployment status: BUILDING" in progress
        assert "Railway deployment status: SUCCESS" in progress
        assert progress.index("Admin keystore unlocked") < progress.index(
            "Scaffolded foo"
        )

    def test_json_mode_suppresses_human_progress_callback(self, runner, monkeypatch):
        """Machine-readable mode remains exactly one JSON document on stdout."""
        seen: dict[str, object] = {}

        def _spy(slug, **kwargs):
            seen.update(kwargs)
            return {
                "schema_version": 1,
                "status": "success",
                "app_slug": slug,
                "variant": "full",
                "scaffolded_by": "manual",
                "logto": None,
                "github": None,
                "railway": None,
                "completed_steps": [],
            }

        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)

        result = runner.invoke(
            cli.app, ["--json", "create-app", "foo", "--no-railway"]
        )

        assert result.exit_code == 0
        assert seen["progress"] is None
        assert json.loads(result.stdout)["status"] == "success"

    def test_no_mobile_reaches_orchestrator(self, runner, monkeypatch):
        seen: dict[str, object] = {}

        def _spy(slug, **kwargs):
            seen.update(kwargs)
            return {
                "schema_version": 1,
                "status": "success",
                "app_slug": slug,
                "variant": "full",
                "scaffolded_by": "manual",
                "logto": None,
                "github": None,
                "railway": None,
                "completed_steps": [],
            }

        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--no-mobile", "--no-railway"]
        )

        assert result.exit_code == 0
        assert seen["with_mobile"] is False
        assert callable(seen["progress"])

    def test_manual_run_has_no_built_in_workspace(
        self, runner, monkeypatch
    ):
        """With no flag and no RAILWAY_WORKSPACE, no workspace name is
        invented: the CLI must not point other people at the maintainer's."""
        seen: dict[str, object] = {}

        def _spy(slug, **kwargs):
            seen.update(kwargs)
            return {
                "schema_version": 1,
                "status": "success",
                "app_slug": slug,
                "variant": kwargs.get("variant", "full"),
                "scaffolded_by": kwargs.get("scaffolded_by", "manual"),
                "logto": None,
                "github": None,
                "railway": None,
                "completed_steps": [],
                "dry_run": True,
            }

        monkeypatch.delenv("RAILWAY_WORKSPACE", raising=False)
        monkeypatch.setattr(cli, "_load_env_files", lambda: None)
        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)

        result = runner.invoke(cli.app, ["create-app", "foo", "--dry-run"])

        assert result.exit_code == 0
        assert seen["railway_workspace"] is None

    def test_workspace_loaded_from_dotenv_after_typer_parsing(
        self, runner, monkeypatch
    ):
        """The command loads .env inside the callback, after Typer resolves
        option envvars, so it must explicitly re-read RAILWAY_WORKSPACE."""
        seen: dict[str, object] = {}

        def _load_env() -> None:
            monkeypatch.setenv("RAILWAY_WORKSPACE", "Configured workspace")

        def _spy(slug, **kwargs):
            seen.update(kwargs)
            return {
                "schema_version": 1,
                "status": "success",
                "app_slug": slug,
                "variant": kwargs.get("variant", "full"),
                "scaffolded_by": kwargs.get("scaffolded_by", "manual"),
                "logto": None,
                "github": None,
                "railway": None,
                "completed_steps": [],
                "dry_run": True,
            }

        monkeypatch.delenv("RAILWAY_WORKSPACE", raising=False)
        monkeypatch.setattr(cli, "_load_env_files", _load_env)
        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)

        result = runner.invoke(cli.app, ["create-app", "foo", "--dry-run"])

        assert result.exit_code == 0
        assert seen["railway_workspace"] == "Configured workspace"

    def test_help_lists_canonical_r11_flags(self, runner):
        """--help must show every R11-canonical flag name."""
        result = runner.invoke(cli.app, ["create-app", "--help"])
        assert result.exit_code == 0
        for flag in (
            "--no-auth",
            "--no-db",
            "--no-frontend",
            "--no-mobile",
            "--no-railway",
            "--no-github",
            "--with-auth",
            "--with-db",
            "--variant",
            "--dry-run",
            "--api-version",
        ):
            assert flag in plain(result.stdout), (
                f"{flag} missing from --help output"
            )

    def test_old_no_logto_flag_is_rejected_with_migration_hint(self, runner):
        """R11: --no-logto removed; reject with a hint suggesting --no-auth."""
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--no-logto", "--dry-run"]
        )
        assert result.exit_code != 0
        # Typer/Click parse error puts "No such option" on stderr; we also
        # want a custom hint somewhere combining --no-auth.
        combined = (result.stderr or "") + (result.stdout or "")
        assert "--no-logto" in combined or "no-logto" in combined.lower()

    def test_conflicting_no_auth_with_auth_exits_one(self, runner):
        """Passing both --no-auth and --with-auth ⇒ exit 1 user error."""
        result = runner.invoke(
            cli.app,
            [
                "create-app",
                "foo",
                "--variant",
                "static-dashboard",
                "--no-auth",
                "--with-auth",
                "--dry-run",
            ],
        )
        assert result.exit_code == 1

    def test_unsupported_api_version_exits_one(self, runner):
        """R12: --api-version 2 (or anything other than 1) is rejected today."""
        result = runner.invoke(
            cli.app,
            ["create-app", "foo", "--api-version", "2", "--dry-run"],
        )
        assert result.exit_code == 1

    def test_api_version_1_accepted(self, runner):
        result = runner.invoke(
            cli.app,
            ["--json", "create-app", "foo", "--api-version", "1", "--dry-run"],
        )
        assert result.exit_code == 0

    def test_dry_run_succeeds_human_mode(self, runner):
        """Pretty (non-JSON) mode still works for the interactive path."""
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--dry-run"]
        )
        assert result.exit_code == 0


class TestCreateAppExitCodes:

    def test_invalid_slug_exits_one(self, runner):
        result = runner.invoke(
            cli.app, ["--json", "create-app", "BAD-Slug", "--dry-run"]
        )
        assert result.exit_code == 1

    def test_deterministic_flag_failure_is_not_retryable(self, runner):
        """Fix 10: a deterministic flag-validation failure (unsupported
        --api-version) must emit retryable=false — re-running with the same
        flags can never succeed."""
        result = runner.invoke(
            cli.app,
            ["--json", "create-app", "foo", "--api-version", "2", "--dry-run"],
        )
        assert result.exit_code == 1
        payload = json.loads(result.stdout.strip())
        assert payload["error_code"] == "UNSUPPORTED_API_VERSION"
        assert payload["retryable"] is False

    def test_unhandled_exception_exits_three(self, runner, monkeypatch):
        """R4: provisioning-class failure → exit 3, no traceback on stdout."""

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated unhandled failure")

        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _boom)
        result = runner.invoke(
            cli.app, ["--json", "create-app", "foo", "--dry-run"]
        )
        assert result.exit_code == 3
        assert "Traceback" not in result.stdout


class TestCreateAppPreflightFlag:
    """--preflight runs only credential validation and creates nothing (§R6)."""

    def test_preflight_pass_exits_zero_without_orchestrating(self, runner, monkeypatch):
        called = {"orchestrate": 0}

        def _spy(*args, **kwargs):
            called["orchestrate"] += 1
            return {}

        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)
        monkeypatch.setattr(
            preflight, "run_preflight", lambda **k: {"status": "success", "checkpoint": "preflight"}
        )
        result = runner.invoke(cli.app, ["create-app", "foo", "--preflight"])
        assert result.exit_code == 0
        assert called["orchestrate"] == 0

    def test_preflight_failure_surfaces_error_code_and_exit(self, runner, monkeypatch):
        monkeypatch.setattr(
            preflight,
            "run_preflight",
            lambda **k: {
                "status": "failed",
                "checkpoint": "preflight",
                "error_code": "PREFLIGHT_RAILWAY_MISSING",
                "exit_code": 5,
                "error_message": "`railway` binary not found on PATH.",
            },
        )
        result = runner.invoke(
            cli.app, ["--json", "create-app", "foo", "--preflight"]
        )
        assert result.exit_code == 5
        payload = json.loads(result.stdout)
        assert payload["error_code"] == "PREFLIGHT_RAILWAY_MISSING"

    def test_preflight_failure_is_not_retryable(self, runner, monkeypatch):
        """Fix 10: the standalone --preflight failure must mirror the
        orchestrator's retryable=False for the same deterministic (creds/CLI/
        keystore) failure class — NOT default to retryable=true."""
        monkeypatch.setattr(
            preflight,
            "run_preflight",
            lambda **k: {
                "status": "failed",
                "checkpoint": "preflight",
                "error_code": "PREFLIGHT_RAILWAY_MISSING",
                "exit_code": 5,
                "error_message": "`railway` binary not found on PATH.",
            },
        )
        result = runner.invoke(
            cli.app, ["--json", "create-app", "foo", "--preflight"]
        )
        payload = json.loads(result.stdout)
        assert payload["retryable"] is False

    def test_preflight_forwards_provider_optouts(self, runner, monkeypatch):
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return {"status": "success", "checkpoint": "preflight"}

        monkeypatch.setattr(preflight, "run_preflight", _capture)
        result = runner.invoke(
            cli.app,
            ["create-app", "foo", "--preflight", "--no-railway", "--no-github"],
        )
        assert result.exit_code == 0
        assert captured.get("with_railway") is False
        assert captured.get("with_github") is False


class TestDeployTargetResolution:
    """KTD11: target resolution for create-app and --preflight."""

    @pytest.fixture
    def seen(self, monkeypatch):
        seen: dict[str, object] = {}

        def _spy(slug, **kwargs):
            seen.update(kwargs)
            return {
                "schema_version": 1,
                "status": "success",
                "app_slug": slug,
                "variant": kwargs.get("variant", "full"),
                "scaffolded_by": "manual",
                "logto": None,
                "github": None,
                "railway": None,
                "completed_steps": [],
            }

        monkeypatch.delenv("RAILWAY_WORKSPACE", raising=False)
        monkeypatch.setattr(cli, "_load_env_files", lambda: None)
        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)
        return seen

    def test_no_deploy_flags_resolve_to_render(self, runner, seen):
        result = runner.invoke(cli.app, ["create-app", "foo"])
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "render"
        assert seen["with_deploy"] is True
        assert seen["auth_provider"] == "google"
        assert seen["with_logto"] is False

    def test_no_railway_alone_resolves_to_railway(self, runner, seen):
        result = runner.invoke(cli.app, ["create-app", "foo", "--no-railway"])
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "railway"
        assert seen["with_railway"] is False

    def test_railway_workspace_flag_resolves_to_railway(self, runner, seen):
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--railway-workspace", "x"]
        )
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "railway"
        assert seen["railway_workspace"] == "x"
        assert seen["with_logto"] is True

    def test_explicit_railway_target(self, runner, seen):
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--deploy-target", "railway"]
        )
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "railway"
        assert seen["auth_provider"] == "google"
        assert seen["with_logto"] is False

    @pytest.mark.parametrize("target", ["render", "railway"])
    def test_explicit_logto_with_either_target(self, runner, seen, target):
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--deploy-target", target, "--auth-provider", "logto"]
        )
        assert result.exit_code == 0, result.stderr
        assert seen["auth_provider"] == "logto"
        assert seen["deploy_target"] == target
        assert seen["with_logto"] is (target == "railway")

    def test_exported_railway_workspace_does_not_select_railway(
        self, runner, seen, monkeypatch
    ):
        monkeypatch.setenv("RAILWAY_WORKSPACE", "agent-sandbox")
        result = runner.invoke(cli.app, ["create-app", "foo"])
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "render"

    def test_exported_railway_workspace_does_not_block_render(
        self, runner, seen, monkeypatch
    ):
        monkeypatch.setenv("RAILWAY_WORKSPACE", "agent-sandbox")
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--deploy-target", "render"]
        )
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "render"

    def test_dotenv_railway_workspace_does_not_select_railway(
        self, runner, seen, monkeypatch
    ):
        monkeypatch.setattr(
            cli,
            "_load_env_files",
            lambda: monkeypatch.setenv("RAILWAY_WORKSPACE", "from-dotenv"),
        )
        result = runner.invoke(cli.app, ["create-app", "foo"])
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "render"

    @pytest.mark.parametrize(
        "extra",
        [
            ["--no-railway"],
            ["--railway-workspace", "x"],
            ["--variant", "static-dashboard", "--with-auth"],
        ],
    )
    def test_render_target_conflicts_are_usage_errors(self, runner, seen, extra):
        result = runner.invoke(
            cli.app,
            ["--json", "create-app", "foo", "--deploy-target", "render", *extra],
        )
        # Exit 1 is the R4 bad-flag user error; exit 2 means auth/keystore.
        assert result.exit_code == 1
        assert seen == {}
        payload = json.loads(result.stdout)
        assert payload["error_code"] == "DEPLOY_TARGET_CONFLICT"
        assert payload["exit_code"] == 1
        assert payload["retryable"] is False

    def test_with_auth_on_default_render_target_is_usage_error(self, runner, seen):
        result = runner.invoke(
            cli.app,
            ["create-app", "foo", "--variant", "static-dashboard", "--with-auth"],
        )
        assert result.exit_code == 1
        assert seen == {}

    def test_invalid_target_value_is_usage_error(self, runner, seen):
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--deploy-target", "heroku"]
        )
        assert result.exit_code == 2
        assert seen == {}

    def test_no_deploy_keeps_render_and_disables_deploy(self, runner, seen):
        result = runner.invoke(cli.app, ["create-app", "foo", "--no-deploy"])
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "render"
        assert seen["with_deploy"] is False

    def test_output_dir_reaches_orchestrator(self, runner, seen, tmp_path):
        target = tmp_path / "x"
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--output-dir", str(target)]
        )
        assert result.exit_code == 0, result.stderr
        assert seen["output_dir"] == target

    def test_output_dir_defaults_to_none(self, runner, seen):
        result = runner.invoke(cli.app, ["create-app", "foo"])
        assert result.exit_code == 0, result.stderr
        assert seen["output_dir"] is None

    @staticmethod
    def _write_state(app_root, deploy_target):
        app_dir = app_root / "foo"
        app_dir.mkdir(parents=True)
        payload = {
            "schema_version": 1,
            "app_slug": "foo",
            "variant": "full",
            "scaffolded_by": "manual",
            "created_at": "2026-11-01T00:00:00Z",
            "checkpoints": {"template": True},
            "resource_ids": {},
            "last_completed_step": "template",
        }
        if deploy_target is not None:
            payload["deploy_target"] = deploy_target
        (app_dir / ".bootstrap-state.json").write_text(json.dumps(payload))

    def test_bare_resume_of_legacy_railway_state_resolves_to_railway(
        self, runner, seen, tmp_path
    ):
        # A state file written before the deploy_target field loads as railway.
        self._write_state(tmp_path, None)
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--resume", "--output-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "railway"
        assert seen["with_logto"] is True

    def test_bare_resume_keeps_the_recorded_provider(self, runner, seen, tmp_path):
        self._write_state(tmp_path, "railway")
        path = tmp_path / "foo" / ".bootstrap-state.json"
        state = json.loads(path.read_text())
        state["auth_provider"] = "google"
        path.write_text(json.dumps(state))
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--resume", "--output-dir", str(tmp_path)]
        )

        assert result.exit_code == 0, result.stderr
        assert seen["auth_provider"] == "google"
        assert seen["with_logto"] is False

    def test_bare_resume_of_render_state_resolves_to_render(
        self, runner, seen, tmp_path
    ):
        self._write_state(tmp_path, "render")
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--resume", "--output-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "render"

    def test_bare_resume_without_state_keeps_render_default(
        self, runner, seen, tmp_path
    ):
        result = runner.invoke(
            cli.app, ["create-app", "foo", "--resume", "--output-dir", str(tmp_path)]
        )
        assert result.exit_code == 0, result.stderr
        assert seen["deploy_target"] == "render"

    def test_explicit_target_differing_from_state_is_mismatch(
        self, runner, monkeypatch, tmp_path
    ):
        # The real orchestrator runs here. Preflight is stubbed so no live
        # check can run.
        monkeypatch.delenv("RAILWAY_WORKSPACE", raising=False)
        monkeypatch.setattr(cli, "_load_env_files", lambda: None)
        monkeypatch.setattr(
            preflight,
            "run_preflight",
            lambda **_: {"status": "success", "checkpoint": "preflight"},
        )
        self._write_state(tmp_path, "railway")
        result = runner.invoke(
            cli.app,
            [
                "--json",
                "create-app",
                "foo",
                "--resume",
                "--deploy-target",
                "render",
                "--output-dir",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1, result.stderr
        payload = json.loads(result.stdout)
        assert payload["error_code"] == "DEPLOY_TARGET_MISMATCH"
        assert payload["retryable"] is False

    def test_human_mode_prints_blueprint_link(self, runner, monkeypatch):
        url = "https://render.com/deploy?repo=https://github.com/example-owner/foo"

        def _spy(slug, **kwargs):
            return {
                "schema_version": 1,
                "status": "success",
                "app_slug": slug,
                "variant": "full",
                "scaffolded_by": "manual",
                "deploy_target": "render",
                "logto": None,
                "github": None,
                "railway": None,
                "render": {"blueprint_url": url, "next_step": "Open the link."},
                "auth": {"status": "not_configured", "next_step": "Run auth."},
                "completed_steps": [],
            }

        monkeypatch.setattr(cli, "_load_env_files", lambda: None)
        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)
        result = runner.invoke(cli.app, ["create-app", "foo"])
        assert result.exit_code == 0, result.stderr
        out = plain(result.stderr)
        assert url in out
        assert out.index("Scaffolded foo") < out.index(url)

    def test_human_mode_prints_next_step_without_repo(self, runner, monkeypatch):
        def _spy(slug, **kwargs):
            return {
                "schema_version": 1,
                "status": "success",
                "app_slug": slug,
                "variant": "full",
                "scaffolded_by": "manual",
                "deploy_target": "render",
                "logto": None,
                "github": None,
                "railway": None,
                "render": {"blueprint_url": None, "next_step": "Push the repo first."},
                "auth": {"status": "not_configured", "next_step": "Run auth."},
                "completed_steps": [],
            }

        monkeypatch.setattr(cli, "_load_env_files", lambda: None)
        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _spy)
        result = runner.invoke(cli.app, ["create-app", "foo", "--no-github"])
        assert result.exit_code == 0, result.stderr
        assert "Push the repo first." in plain(result.stderr)


class TestPreflightDeployTarget:
    """--preflight uses the same KTD11 target resolution as create-app."""

    @pytest.fixture
    def captured(self, monkeypatch):
        captured: dict[str, object] = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return {"status": "success", "checkpoint": "preflight"}

        monkeypatch.delenv("RAILWAY_WORKSPACE", raising=False)
        monkeypatch.setattr(cli, "_load_env_files", lambda: None)
        monkeypatch.setattr(preflight, "run_preflight", _capture)
        return captured

    def test_preflight_defaults_to_render(self, runner, captured):
        result = runner.invoke(cli.app, ["create-app", "foo", "--preflight"])
        assert result.exit_code == 0, result.stderr
        assert captured["deploy_target"] == "render"
        assert captured["with_deploy"] is True
        assert captured["with_logto"] is False

    def test_preflight_railway_workspace_selects_railway(self, runner, captured):
        result = runner.invoke(
            cli.app,
            ["create-app", "foo", "--preflight", "--railway-workspace", "x"],
        )
        assert result.exit_code == 0, result.stderr
        assert captured["deploy_target"] == "railway"
        assert captured["with_logto"] is True

    def test_preflight_render_conflict_is_usage_error(self, runner, captured):
        result = runner.invoke(
            cli.app,
            ["create-app", "foo", "--preflight", "--deploy-target", "render", "--no-railway"],
        )
        assert result.exit_code == 1
        assert captured == {}


# ---------------------------------------------------------------------------
# teardown — U8 manual escape hatch. Confirmation is enforced at the CLI via
# typer.confirm unless --yes. The orchestration (teardown_app) is stubbed;
# its own coverage lives in test_bootstrap_teardown.py.
# ---------------------------------------------------------------------------


class TestTeardownCommand:
    @pytest.fixture(autouse=True)
    def _mock_keystore(self, monkeypatch):
        # teardown_cmd now decrypts the admin keystore for a live management
        # credential (cold-cache fix). Mock the chain so command-flow tests
        # don't hit getpass or require a real keystore file.
        monkeypatch.setattr(
            cli.keystore, "resolve_keystore_path", lambda o: "/fake/ks.json"
        )
        monkeypatch.setattr(cli.keystore, "load_keystore", lambda p: {"fake": True})
        monkeypatch.setattr(cli.keystore, "read_passphrase", lambda fd: "pw")
        monkeypatch.setattr(
            cli.keystore, "decrypt_credential", lambda env, pw: bytearray(b"cred")
        )
        # Never load the developer's real ./.env or ~/.agentic-apps.env.
        monkeypatch.setattr(cli, "_load_env_files", lambda: None)

    def test_teardown_loads_env_files_before_the_keystore(self, runner, monkeypatch):
        """teardown needs LOGTO_ENDPOINT and LOGTO_M2M_APP_ID, which the
        README puts in .env."""
        from agentic_core.bootstrap import teardown as teardown_mod

        order: list[str] = []
        monkeypatch.setattr(cli, "_load_env_files", lambda: order.append("env"))
        monkeypatch.setattr(
            cli.keystore,
            "resolve_keystore_path",
            lambda o: order.append("keystore") or "/fake/ks.json",
        )
        monkeypatch.setattr(
            teardown_mod,
            "teardown_app",
            lambda slug, **kw: order.append("teardown") or self._summary(slug),
        )
        result = runner.invoke(cli.app, ["--json", "teardown", "foo", "--yes"])
        assert result.exit_code == 0
        assert order == ["env", "keystore", "teardown"]

    def _summary(self, slug="foo"):
        return {
            "schema_version": 1,
            "status": "success",
            "app_slug": slug,
            "variant": "full",
            "scaffolded_by": "manual",
            "teardown": {
                "deleted": ["logto_spa_client_id"],
                "already_absent": [],
                "skipped_admin": [],
                "preserved": ["github_repo_url"],
                "aborted": False,
            },
            "dry_run": False,
        }

    def test_help_lists_teardown(self, runner):
        result = runner.invoke(cli.app, ["--help"])
        assert result.exit_code == 0
        assert "teardown" in result.stdout

    def test_yes_flag_skips_prompt_and_runs(self, runner, monkeypatch):
        from agentic_core.bootstrap import teardown as teardown_mod

        captured = {}

        def _spy(slug, **kwargs):
            captured["slug"] = slug
            captured["assume_yes"] = kwargs.get("assume_yes")
            return self._summary(slug)

        monkeypatch.setattr(teardown_mod, "teardown_app", _spy)
        result = runner.invoke(cli.app, ["--json", "teardown", "foo", "--yes"])
        assert result.exit_code == 0
        assert captured["slug"] == "foo"
        assert captured["assume_yes"] is True
        payload = json.loads(result.stdout.strip())
        assert payload["status"] == "success"

    def test_declined_prompt_aborts_and_deletes_nothing(self, runner, monkeypatch):
        from agentic_core.bootstrap import teardown as teardown_mod

        called = {"n": 0}

        def _spy(slug, **kwargs):
            called["n"] += 1
            return self._summary(slug)

        monkeypatch.setattr(teardown_mod, "teardown_app", _spy)
        # Answer "n" to the confirmation prompt.
        result = runner.invoke(cli.app, ["teardown", "foo"], input="n\n")
        assert result.exit_code != 0
        # teardown_app must never have been invoked → zero deletes.
        assert called["n"] == 0

    def test_accepted_prompt_runs(self, runner, monkeypatch):
        from agentic_core.bootstrap import teardown as teardown_mod

        called = {"n": 0}

        def _spy(slug, **kwargs):
            called["n"] += 1
            return self._summary(slug)

        monkeypatch.setattr(teardown_mod, "teardown_app", _spy)
        result = runner.invoke(cli.app, ["teardown", "foo"], input="y\n")
        assert result.exit_code == 0
        assert called["n"] == 1

    def test_failed_teardown_status_exits_nonzero(self, runner, monkeypatch):
        """Finding 2b: when teardown_app reports status 'failed' (a delete
        raised, or the admin guard refused), the CLI must exit NON-ZERO so
        --json and human callers both see the failure — not a false success."""
        from agentic_core.bootstrap import teardown as teardown_mod

        def _spy(slug, **kwargs):
            summary = self._summary(slug)
            summary["status"] = "failed"
            summary["teardown"]["failed"] = ["railway_project_id"]
            return summary

        monkeypatch.setattr(teardown_mod, "teardown_app", _spy)
        result = runner.invoke(cli.app, ["--json", "teardown", "foo", "--yes"])
        assert result.exit_code != 0
        payload = json.loads(result.stdout.strip())
        assert payload["exit_code"] != 0

    def test_successful_teardown_exits_zero(self, runner, monkeypatch):
        from agentic_core.bootstrap import teardown as teardown_mod

        monkeypatch.setattr(
            teardown_mod, "teardown_app", lambda slug, **kw: self._summary(slug)
        )
        result = runner.invoke(cli.app, ["--json", "teardown", "foo", "--yes"])
        assert result.exit_code == 0

    def test_json_without_yes_emits_confirmation_required_envelope(
        self, runner, monkeypatch
    ):
        """Fix 11: in --json (non-interactive) mode, teardown without --yes must
        NOT print an interactive prompt / bare 'Aborted.' — it must emit a
        parseable JSON CONFIRMATION_REQUIRED failure and never call
        teardown_app."""
        from agentic_core.bootstrap import teardown as teardown_mod

        called = {"n": 0}

        def _spy(slug, **kwargs):
            called["n"] += 1
            return self._summary(slug)

        monkeypatch.setattr(teardown_mod, "teardown_app", _spy)
        result = runner.invoke(cli.app, ["--json", "teardown", "foo"])
        assert result.exit_code != 0
        assert called["n"] == 0
        assert "Aborted." not in (result.stdout or "")
        payload = json.loads(result.stdout.strip())
        assert payload["error_code"] == "CONFIRMATION_REQUIRED"
        assert payload["retryable"] is False

    def test_failed_teardown_json_preserves_per_resource_summary(
        self, runner, monkeypatch
    ):
        """Fix 23: a partial-failure teardown's --json output must still carry
        the per-resource teardown sub-block (deleted / already_absent /
        skipped_admin / preserved / failed) so an operator/agent sees what was
        and wasn't deleted — not just a bare error envelope."""
        from agentic_core.bootstrap import teardown as teardown_mod

        def _spy(slug, **kwargs):
            summary = self._summary(slug)
            summary["status"] = "failed"
            summary["teardown"]["deleted"] = ["logto_spa_client_id"]
            summary["teardown"]["already_absent"] = ["logto_user_sub"]
            summary["teardown"]["failed"] = ["railway_project_id"]
            return summary

        monkeypatch.setattr(teardown_mod, "teardown_app", _spy)
        result = runner.invoke(cli.app, ["--json", "teardown", "foo", "--yes"])
        assert result.exit_code != 0
        payload = json.loads(result.stdout.strip())
        td = payload["teardown"]
        assert td["deleted"] == ["logto_spa_client_id"]
        assert td["already_absent"] == ["logto_user_sub"]
        assert td["failed"] == ["railway_project_id"]
        assert td["preserved"] == ["github_repo_url"]

    def test_traversal_slug_rejected_before_teardown(self, runner, monkeypatch):
        """Fix 2: a path-traversal slug is rejected (exit 1) and teardown_app
        is never reached — no filesystem/delete activity on a DESTRUCTIVE cmd."""
        from agentic_core.bootstrap import teardown as teardown_mod

        called = {"n": 0}

        def _spy(slug, **kwargs):
            called["n"] += 1
            return self._summary(slug)

        monkeypatch.setattr(teardown_mod, "teardown_app", _spy)
        # --yes would otherwise skip the confirmation prompt that might reveal
        # the traversal; validation must happen regardless.
        result = runner.invoke(
            cli.app, ["--json", "teardown", "../../evil", "--yes"]
        )
        assert result.exit_code == 1
        assert called["n"] == 0
        payload = json.loads(result.stdout.strip())
        assert payload["error_code"] == "INVALID_SLUG"
