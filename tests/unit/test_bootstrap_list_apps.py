"""Unit tests for ``agentic-core list-apps`` registry lister (task #10).

Verifies the §R18 registry-only contract:

  - Scans the parent directory for sibling dirs containing
    ``.bootstrap-state.json``.
  - Each entry mirrors the state file: slug, variant, scaffolded_by,
    logto/github/railway sub-blocks (built from ``resource_ids``), created_at.
  - NO live status checks (no HTTP, no shelling out to ``gh``/``railway``).
  - JSON output validates against :class:`ListAppsEnvelope`.
  - Missing parent (or no siblings) returns empty list, exit 0.
  - Corrupt state files are skipped with a stderr warning (other apps still
    listed) — registry is best-effort and read-only.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentic_core import cli, list_apps
from agentic_core.bootstrap import state as state_mod
from agentic_core.output_schema import ListAppsEnvelope


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_state(
    sibling_root: Path,
    slug: str,
    *,
    variant: str = "full",
    scaffolded_by: str = "manual",
    resource_ids: dict[str, str] | None = None,
) -> Path:
    """Write a stub ``.bootstrap-state.json`` for a sibling at ``slug``."""
    app_dir = sibling_root / slug
    app_dir.mkdir(parents=True, exist_ok=True)
    state = state_mod.BootstrapState(
        app_slug=slug,
        variant=variant,
        scaffolded_by=scaffolded_by,  # type: ignore[arg-type]
        created_at=datetime.now(UTC),
        resource_ids=resource_ids or {},
    )
    state_path = app_dir / ".bootstrap-state.json"
    state_mod.write_state(state_path, state)
    return state_path


class TestScanRegistry:
    """Pure scan logic — no CLI involved."""

    def test_empty_parent_returns_empty_list(self, tmp_path):
        env = list_apps.scan_registry(tmp_path)
        assert env.apps == []
        assert env.scanned_dir == str(tmp_path)

    def test_missing_parent_returns_empty_list(self, tmp_path):
        ghost = tmp_path / "does-not-exist"
        env = list_apps.scan_registry(ghost)
        assert env.apps == []

    def test_single_app_listed(self, tmp_path):
        _write_state(tmp_path, "habits-tracker", variant="full")
        env = list_apps.scan_registry(tmp_path)
        assert len(env.apps) == 1
        assert env.apps[0].slug == "habits-tracker"
        assert env.apps[0].variant == "full"
        assert env.apps[0].scaffolded_by == "manual"

    def test_two_apps_listed_against_tmp_dir(self, tmp_path):
        """AC: list-apps against tmp dir with 2 state files returns 2."""
        _write_state(tmp_path, "alpha", variant="full", scaffolded_by="agent")
        _write_state(
            tmp_path, "bravo", variant="lightweight-api", scaffolded_by="manual"
        )
        env = list_apps.scan_registry(tmp_path)
        slugs = sorted(a.slug for a in env.apps)
        assert slugs == ["alpha", "bravo"]

    def test_dirs_without_state_file_skipped(self, tmp_path):
        _write_state(tmp_path, "real-app")
        (tmp_path / "not-an-app").mkdir()
        (tmp_path / "not-an-app" / "README.md").write_text("# not an app")
        env = list_apps.scan_registry(tmp_path)
        assert [a.slug for a in env.apps] == ["real-app"]

    def test_logto_block_built_from_resource_ids(self, tmp_path):
        _write_state(
            tmp_path,
            "ha",
            resource_ids={
                "logto_spa_client_id": "abc123spa",
                "logto_m2m_app_id": "m2mxyz",
                "logto_api_resource": "https://ha.example.com/api",
            },
        )
        env = list_apps.scan_registry(tmp_path)
        entry = env.apps[0]
        assert entry.logto is not None
        assert entry.logto["spa_client_id"] == "abc123spa"
        assert entry.logto["m2m_app_id"] == "m2mxyz"
        assert entry.logto["api_resource"] == "https://ha.example.com/api"

    def test_github_block_built_from_resource_ids(self, tmp_path):
        _write_state(
            tmp_path,
            "ha",
            resource_ids={"github_repo_url": "https://github.com/matt/ha"},
        )
        env = list_apps.scan_registry(tmp_path)
        entry = env.apps[0]
        assert entry.github is not None
        assert entry.github["repo_url"] == "https://github.com/matt/ha"

    def test_no_logto_when_no_logto_resource_ids(self, tmp_path):
        _write_state(
            tmp_path, "ha", resource_ids={"github_repo_url": "https://x/y"}
        )
        env = list_apps.scan_registry(tmp_path)
        assert env.apps[0].logto is None

    def test_corrupt_state_file_skipped(self, tmp_path):
        _write_state(tmp_path, "good-one")
        bad_dir = tmp_path / "broken"
        bad_dir.mkdir()
        (bad_dir / ".bootstrap-state.json").write_text("{not json")
        env = list_apps.scan_registry(tmp_path)
        # Corrupt one skipped, healthy one still listed.
        assert [a.slug for a in env.apps] == ["good-one"]

    def test_state_file_path_recorded(self, tmp_path):
        _write_state(tmp_path, "ha")
        env = list_apps.scan_registry(tmp_path)
        entry = env.apps[0]
        assert entry.state_file.endswith(".bootstrap-state.json")
        assert "ha" in entry.state_file


class TestEnvelopeValidation:
    """AC: list-apps JSON validates against ListAppsEnvelope Pydantic model."""

    def test_envelope_round_trips_through_pydantic(self, tmp_path):
        _write_state(tmp_path, "alpha", scaffolded_by="agent")
        _write_state(tmp_path, "bravo", scaffolded_by="manual")
        env = list_apps.scan_registry(tmp_path)
        # Already a Pydantic model — round-trip via JSON to verify shape.
        payload = env.model_dump_json()
        reloaded = ListAppsEnvelope.model_validate_json(payload)
        assert len(reloaded.apps) == 2

    def test_envelope_status_is_success(self, tmp_path):
        env = list_apps.scan_registry(tmp_path)
        assert env.status == "success"

    def test_envelope_schema_version_is_one(self, tmp_path):
        env = list_apps.scan_registry(tmp_path)
        assert env.schema_version == 1


class TestListAppsCli:
    """End-to-end via CliRunner — JSON line on stdout."""

    def test_cli_emits_json_envelope(self, tmp_path, monkeypatch, runner):
        # Force the scan target by monkeypatching the resolver to tmp_path.
        _write_state(tmp_path, "alpha")
        _write_state(tmp_path, "bravo")
        monkeypatch.setattr(
            list_apps, "_resolve_sibling_root", lambda override=None: tmp_path
        )

        result = runner.invoke(cli.app, ["list-apps"])
        assert result.exit_code == 0, result.stdout + (result.stderr or "")
        payload = json.loads(result.stdout.strip())
        assert payload["status"] == "success"
        assert payload["schema_version"] == 1
        assert len(payload["apps"]) == 2

    def test_cli_handles_missing_dir_gracefully(self, tmp_path, monkeypatch, runner):
        ghost = tmp_path / "ghost"
        monkeypatch.setattr(
            list_apps, "_resolve_sibling_root", lambda override=None: ghost
        )
        result = runner.invoke(cli.app, ["list-apps"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout.strip())
        assert payload["apps"] == []

    def test_cli_root_dir_flag_overrides_resolver(self, tmp_path, runner):
        """``--root-dir`` is the explicit override; should not need monkeypatch."""
        _write_state(tmp_path, "alpha")
        result = runner.invoke(cli.app, ["list-apps", "--root-dir", str(tmp_path)])
        assert result.exit_code == 0
        payload = json.loads(result.stdout.strip())
        assert len(payload["apps"]) == 1
        assert payload["apps"][0]["slug"] == "alpha"
