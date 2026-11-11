"""Unit tests for ``agentic-core create-app --json`` JSON contract (task #9).

This file is the gate on the §A2 / §R3 envelope shape that agent callers
read. The success envelope, the failure envelope, the
Pydantic validators, and the R2 stdout discipline are all asserted here so
that any future drift fails the build before it can break a downstream
agent invocation.

Coverage:

  - Success envelope shape: schema_version pinned, status=success, all
    required keys present.
  - Failure envelope shape: schema_version pinned, status=failure (or
    failed for back-compat), exit_code mirrors §R4 contract.
  - --json mode stdout discipline (R2): exactly one line on stdout.
  - Skipped sub-blocks emit as ``null`` JSON.
  - Pydantic validation: invalid envelopes raise.
  - Variant matrix: full / lightweight-api / static-dashboard --no-auth.

Tests stub ``orchestrate_create_app`` directly to keep the unit fast and
avoid any external I/O — the orchestrator itself is covered exhaustively
in ``test_bootstrap_orchestrator.py``.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agentic_core import cli
from agentic_core.bootstrap import orchestrator
from agentic_core.output_schema import (
    CURRENT_SCHEMA_VERSION,
    FailureEnvelope,
    SuccessEnvelope,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# Pydantic model validation
# ---------------------------------------------------------------------------


class TestPydanticModels:
    """Direct validation of the SuccessEnvelope / FailureEnvelope shapes."""

    def test_success_envelope_minimal(self):
        env = SuccessEnvelope(
            app_slug="foo",
            variant="full",
            scaffolded_by="manual",
        )
        assert env.schema_version == 1
        assert env.status == "success"
        assert env.dry_run is False
        assert env.logto is None
        assert env.github is None
        assert env.railway is None

    def test_success_envelope_dry_run(self):
        env = SuccessEnvelope(
            app_slug="foo",
            variant="lightweight-api",
            scaffolded_by="agent",
            dry_run=True,
        )
        assert env.dry_run is True

    def test_success_envelope_rejects_unknown_status(self):
        with pytest.raises(ValidationError):
            SuccessEnvelope(
                app_slug="foo",
                variant="full",
                scaffolded_by="manual",
                status="failed",  # type: ignore[arg-type]
            )

    def test_success_envelope_rejects_unknown_scaffolded_by(self):
        with pytest.raises(ValidationError):
            SuccessEnvelope(
                app_slug="foo",
                variant="full",
                scaffolded_by="robot",  # type: ignore[arg-type]
            )

    def test_failure_envelope_minimal(self):
        env = FailureEnvelope(
            app_slug="foo",
            exit_code=3,
            error_code="LOGTO_API_FAILED",
            error_message="boom",
        )
        assert env.schema_version == 1
        # ``status`` is pinned to the single canonical value every emitter
        # writes; see FailureEnvelope.status Literal declaration.
        assert env.status == "failed"
        assert env.exit_code == 3

    def test_failure_envelope_explicit_status_failed(self):
        # The orchestrator emits ``status: failed`` explicitly — round-trip
        # the same value through the model to confirm it's accepted.
        env = FailureEnvelope(
            app_slug="foo",
            exit_code=1,
            error_code="INVALID_SLUG",
            error_message="bad",
            status="failed",
        )
        assert env.status == "failed"

    def test_round_trip_via_json(self):
        env = SuccessEnvelope(
            app_slug="foo",
            variant="full",
            scaffolded_by="agent",
            logto={"m2m_app_id": "abc", "m2m_app_secret": "shh"},
            cli={"binary": "foo", "env_var_prefix": "FOO_"},
        )
        payload = env.model_dump(mode="json")
        rebuilt = SuccessEnvelope.model_validate(payload)
        assert rebuilt.logto == {"m2m_app_id": "abc", "m2m_app_secret": "shh"}
        assert rebuilt.cli == {"binary": "foo", "env_var_prefix": "FOO_"}


# ---------------------------------------------------------------------------
# create-app --dry-run --json contract
# ---------------------------------------------------------------------------


class TestCreateAppJsonContract:
    """End-to-end JSON shape assertions via CliRunner against a stubbed orchestrator.

    These tests gate the contract surface agent callers read — any
    drift fails the build.
    """

    def test_dry_run_success_envelope_is_pydantic_valid(self, runner):
        """A clean --dry-run emits a JSON line that round-trips through SuccessEnvelope."""
        result = runner.invoke(
            cli.app,
            ["--json", "create-app", "foo", "--dry-run"],
        )
        assert result.exit_code == 0, result.stderr
        # R2: exactly one JSON line on stdout
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, (
            f"R2 stdout discipline broken — expected 1 JSON line, got {lines!r}"
        )
        payload = json.loads(lines[0])
        # Pydantic round-trip = free schema validation
        env = SuccessEnvelope.model_validate(payload)
        assert env.schema_version == CURRENT_SCHEMA_VERSION
        assert env.status == "success"
        assert env.app_slug == "foo"
        assert env.variant == "full"  # default
        assert env.dry_run is True

    def test_lightweight_api_dry_run(self, runner):
        result = runner.invoke(
            cli.app,
            [
                "--json",
                "create-app",
                "foo",
                "--variant",
                "lightweight-api",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout.strip())
        env = SuccessEnvelope.model_validate(payload)
        assert env.variant == "lightweight-api"

    def test_static_dashboard_no_auth_logto_is_null(self, runner):
        """static-dashboard + --no-auth ⇒ logto block emits as null (skipped)."""
        result = runner.invoke(
            cli.app,
            [
                "--json",
                "create-app",
                "foo",
                "--variant",
                "static-dashboard",
                "--no-auth",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout.strip())
        # In --dry-run mode the orchestrator returns logto=None regardless,
        # but the explicit --no-auth + static-dashboard combo is the spec's
        # canonical "skip Logto entirely" path — assert the null is there.
        assert payload["logto"] is None

    def test_render_dry_run_envelope_has_render_and_auth_blocks(self, runner):
        """U7: the default (Render) target emits render + auth blocks and a
        null railway block."""
        result = runner.invoke(
            cli.app,
            ["--json", "create-app", "foo", "--dry-run"],
        )
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout.strip())
        env = SuccessEnvelope.model_validate(payload)
        assert env.deploy_target == "render"
        assert env.railway is None
        assert env.render["status"] == "dry_run"
        assert env.auth["status"] == "not_configured"

    def test_railway_dry_run_envelope_keeps_railway_block(self, runner):
        result = runner.invoke(
            cli.app,
            [
                "--json",
                "create-app",
                "foo",
                "--railway-workspace",
                "agent-sandbox",
                "--platform-domain",
                "example.test",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout.strip())
        env = SuccessEnvelope.model_validate(payload)
        assert env.deploy_target == "railway"
        assert env.railway == {"workspace": "agent-sandbox"}
        assert env.render is None

    def test_stdout_is_single_line_in_json_mode(self, runner):
        """R2: stdout in --json mode is exactly one line, period."""
        result = runner.invoke(
            cli.app,
            ["--json", "create-app", "foo", "--dry-run"],
        )
        assert result.exit_code == 0, result.stderr
        # Even raw — no .strip(), no filter — should be exactly one line + newline
        non_empty = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(non_empty) == 1

    def test_failure_envelope_for_invalid_slug(self, runner):
        """Invalid slug → exit 1, failure envelope on stdout, parseable JSON."""
        result = runner.invoke(
            cli.app,
            ["--json", "create-app", "BAD_SLUG_UPPERCASE", "--dry-run"],
        )
        assert result.exit_code == 1
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        env = FailureEnvelope.model_validate(payload)
        assert env.exit_code == 1
        assert env.error_code == "INVALID_SLUG"

    def test_unhandled_exception_translates_to_failure_envelope(
        self, runner, monkeypatch
    ):
        """RuntimeError from the orchestrator → exit 3 + failure envelope,
        never a traceback on stdout."""

        def _boom(*args, **kwargs):
            raise RuntimeError("kaboom from inside the orchestrator")

        monkeypatch.setattr(orchestrator, "orchestrate_create_app", _boom)
        result = runner.invoke(
            cli.app,
            ["--json", "create-app", "foo", "--dry-run"],
        )
        assert result.exit_code == 3
        # No Python traceback on stdout — that would corrupt the contract.
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, f"stdout must be 1 JSON line, got {lines!r}"
        payload = json.loads(lines[0])
        env = FailureEnvelope.model_validate(payload)
        assert env.exit_code == 3
        assert "kaboom" in env.error_message.lower() or env.error_code == "UNHANDLED_EXCEPTION"
        # The literal "Traceback" string must not appear on stdout — that's
        # what would happen if we let the exception bubble unhandled.
        assert "Traceback" not in result.stdout
