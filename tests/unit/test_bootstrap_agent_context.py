"""Unit tests for ``agentic-core agent-context`` envelope (task #10).

Verifies the agent-context v1 envelope:

  - Real Typer introspection populates ``commands`` with create-app, list-apps,
    admin init-keystore, and agent-context entries.
  - ``error_codes`` includes the full R4 contract 0-6 (Success → Rate limited).
  - ``auth`` includes ``passphrase_transport`` per R1 (one of "stdin", "fd").
  - ``auth`` includes ``keystore_path_resolution`` precedence list per R7.
  - ``slug_glossary`` documents slug → APP_UPPER → App-Name-Title transforms
    per CQ-002.

Tests run via Typer's CliRunner so we can assert the JSON line on stdout.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from agentic_core import __version__, agent_context, cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestAgentContextEnvelope:
    """Direct calls to ``agent_context.generate`` — schema shape."""

    def test_generate_returns_schema_version_1(self):
        env = agent_context.generate(cli.app, version=__version__)
        assert env["schema_version"] == 1

    def test_generate_returns_tool_name(self):
        env = agent_context.generate(cli.app, version=__version__)
        assert env["tool"] == "agentic-core"

    def test_generate_returns_version(self):
        env = agent_context.generate(cli.app, version=__version__)
        assert env["version"] == __version__

    def test_commands_includes_create_app(self):
        env = agent_context.generate(cli.app, version=__version__)
        names = [c["name"] for c in env["commands"]]
        assert "create-app" in names

    def test_commands_includes_list_apps(self):
        env = agent_context.generate(cli.app, version=__version__)
        names = [c["name"] for c in env["commands"]]
        assert "list-apps" in names

    def test_commands_includes_agent_context(self):
        env = agent_context.generate(cli.app, version=__version__)
        names = [c["name"] for c in env["commands"]]
        assert "agent-context" in names

    def test_commands_includes_admin_init_keystore(self):
        env = agent_context.generate(cli.app, version=__version__)
        names = [c["name"] for c in env["commands"]]
        # Group-prefixed name (matches per-app template pattern)
        assert any(n.startswith("admin") and "init-keystore" in n for n in names)

    def test_commands_count_at_least_three(self):
        """AC: jq '.commands | length' returns >= 3."""
        env = agent_context.generate(cli.app, version=__version__)
        assert len(env["commands"]) >= 3

    def test_error_codes_includes_all_zero_through_six(self):
        """AC: codes [0,1,2,3,4,5,6] all present with meanings."""
        env = agent_context.generate(cli.app, version=__version__)
        codes = {entry["code"]: entry["meaning"] for entry in env["error_codes"]}
        for c in (0, 1, 2, 3, 4, 5, 6):
            assert c in codes, f"exit code {c} missing from error_codes"
            assert codes[c], f"exit code {c} has empty meaning"

    def test_error_code_six_is_rate_limited(self):
        env = agent_context.generate(cli.app, version=__version__)
        meanings = {e["code"]: e["meaning"] for e in env["error_codes"]}
        assert "rate" in meanings[6].lower()

    def test_auth_passphrase_transport_present(self):
        """AC: R1 auth.passphrase_transport ∈ {"stdin", "fd"}."""
        env = agent_context.generate(cli.app, version=__version__)
        assert "passphrase_transport" in env["auth"]
        transport = env["auth"]["passphrase_transport"]
        # R1: stdin OR fd (both supported by keystore.read_passphrase)
        for token in ("stdin", "fd"):
            assert token in transport.lower(), (
                f"passphrase_transport={transport!r} must mention stdin or fd"
            )

    def test_auth_keystore_path_resolution_present(self):
        """R7 precedence list documented for agents."""
        env = agent_context.generate(cli.app, version=__version__)
        assert "keystore_path_resolution" in env["auth"]
        resolution = env["auth"]["keystore_path_resolution"]
        assert isinstance(resolution, list)
        assert len(resolution) >= 2  # at least flag + env var

    def test_slug_glossary_present_with_three_forms(self):
        """CQ-002: slug → APP_UPPER → App-Name-Title transformations."""
        env = agent_context.generate(cli.app, version=__version__)
        assert "slug_glossary" in env
        glossary = env["slug_glossary"]
        assert "slug" in glossary
        assert "APP_UPPER" in glossary
        assert "App-Name-Title" in glossary


class TestAgentContextCli:
    """End-to-end via CliRunner — JSON line on stdout."""

    def test_runs_and_emits_json(self, runner):
        result = runner.invoke(cli.app, ["agent-context"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout.strip())
        assert payload["tool"] == "agentic-core"
        assert payload["schema_version"] == 1

    def test_emits_commands_list(self, runner):
        result = runner.invoke(cli.app, ["agent-context"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout.strip())
        assert isinstance(payload["commands"], list)
        assert len(payload["commands"]) >= 3

    def test_root_json_flag_works(self, runner):
        """`agentic-core --json agent-context` also emits JSON on stdout."""
        result = runner.invoke(cli.app, ["--json", "agent-context"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout.strip())
        assert payload["tool"] == "agentic-core"
