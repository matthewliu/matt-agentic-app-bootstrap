"""Integration test — generated app's per-app CLI emits the agent-context v1 envelope (task #18).

Renders the mini-app Copier template end-to-end via the in-process wrapper
(``agentic_core.bootstrap.template.render_template``), then exercises the
rendered ``app/cli/`` subpackage two ways:

1.  **Import-based** (always runs, fast): adds the rendered app dir to
    ``sys.path``, imports ``app.cli.main``, drives the Typer app via
    Typer's ``CliRunner``. Asserts the ``agent-context`` and ``version``
    commands exit 0 and that ``agent-context`` JSON validates against the
    agent-context v1 envelope schema (Invariants 4-6 / spec §B2).

2.  **Subprocess install** (gated, slow): creates a venv inside the
    rendered app dir, ``pip install -e .``, invokes the installed
    ``<slug>`` console script as a real binary. This proves the
    ``[project.scripts]`` entry point in ``pyproject.toml`` actually
    wires up at install time — something the import-based test cannot
    detect. Marked ``@pytest.mark.slow`` because pip install is ~30-60s.

Both tests live here per the task scope (two test files, one for CLI, one
for middleware). The fast path is the contract gate; the slow path is the
deploy-time integration gate.

Skip the slow path locally with: ``pytest -m "not slow"``. Run only the
slow path with: ``pytest -m slow``. The scoped task command passes
``--run-slow`` but pytest's default behavior of "unknown markers warn,
tests still run" means the tests run regardless; the marker is purely a
filter knob for humans.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentic_core.bootstrap import template as template_mod


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"

APP_NAME = "cli-integ-app"
APP_DESCRIPTION = "Integration test app for CLI envelope check"


# ---------------------------------------------------------------------------
# Shared fixture: render template once per module, expose rendered dir
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rendered_app_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Render the full-variant template once for this module and return the app dir.

    Uses the in-process Copier wrapper (PERF-004) — sub-second render. The
    same rendered tree is shared by every test in this module; tests must
    not mutate it.
    """
    out = tmp_path_factory.mktemp("cli-integ")
    template_mod.render_template(
        template_dir=TEMPLATE_DIR,
        output_dir=out,
        data={
            "app_name": APP_NAME,
            "app_description": APP_DESCRIPTION,
            "include_frontend": False,
            "include_mobile": False,
            "github_user": "testuser",
            "logto_endpoint": "https://logto.example.com",
            "with_auth": True,
        },
        variant="full",
    )
    app_dir = out / APP_NAME
    assert (app_dir / "app" / "cli" / "main.py").exists(), (
        "render_template did not produce app/cli/main.py — fixture broken"
    )
    return app_dir


@pytest.fixture
def imported_cli(rendered_app_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Import ``app.cli.main`` from the rendered tree and yield the Typer ``app``.

    Cleans up any cached ``app.*`` modules before AND after import so the
    rendered tree is loaded fresh (other tests in the suite may have cached
    a different rendered app's modules under the same ``app`` name).
    """
    # Strip any pre-existing `app` / `app.*` modules from a prior test.
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]

    monkeypatch.syspath_prepend(str(rendered_app_dir))

    # Settings env vars (config.py reads via pydantic-settings). Provide
    # plausible values so import doesn't fail on a stricter Settings init
    # someday — current Settings has defaults for every field, but defense
    # in depth.
    monkeypatch.setenv("LOGTO_ENDPOINT", "https://logto.example.com")
    monkeypatch.setenv("LOGTO_API_RESOURCE", f"https://api.{APP_NAME}.example.com")
    monkeypatch.setenv("LOGTO_M2M_AGENT_APP_ID", "test-m2m-app-id")

    cli_main = importlib.import_module("app.cli.main")
    yield cli_main.app

    # Tear down imported modules so the next test (in this module or others)
    # doesn't get a stale `app.*` from a different rendered tree.
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]


# ---------------------------------------------------------------------------
# Agent-context v1 envelope schema validator (per spec §B2)
# ---------------------------------------------------------------------------


REQUIRED_TOP_LEVEL_KEYS = {
    "schema_version",
    "tool",
    "version",
    "commands",
    "error_codes",
    "auth",
    "cli",
}

REQUIRED_AUTH_KEYS = {"description", "required_env_vars"}

# Per spec §B2: codes 0-5 are the contract every per-app CLI advertises.
EXPECTED_ERROR_CODES = {0, 1, 2, 3, 4, 5}


def _assert_agent_context_v1_envelope(envelope: dict, app_slug: str) -> None:
    """Validate ``envelope`` against the agent-context v1 schema. Raises AssertionError.

    Asserts:
      - top-level keys present (schema_version, tool, version, commands,
        error_codes, auth, cli)
      - schema_version == 1 (integer)
      - tool == app_slug
      - commands is a non-empty list of dicts with name/description/args/options
      - error_codes is a list containing all of {0,1,2,3,4,5}
      - auth has description + required_env_vars
    """
    missing = REQUIRED_TOP_LEVEL_KEYS - set(envelope.keys())
    assert not missing, (
        f"envelope missing required keys {sorted(missing)}; "
        f"got keys {sorted(envelope.keys())}"
    )

    assert envelope["schema_version"] == 1, (
        f"schema_version must be 1, got {envelope['schema_version']!r}"
    )
    assert envelope["tool"] == app_slug, (
        f"tool must be {app_slug!r}, got {envelope['tool']!r}"
    )
    assert isinstance(envelope["version"], str) and envelope["version"], (
        f"version must be a non-empty string, got {envelope['version']!r}"
    )

    commands = envelope["commands"]
    assert isinstance(commands, list), f"commands must be a list, got {type(commands)}"
    assert len(commands) >= 1, "commands list must not be empty"
    for cmd in commands:
        assert isinstance(cmd, dict), f"each command must be a dict, got {type(cmd)}"
        for required_field in ("name", "description", "args", "options"):
            assert required_field in cmd, (
                f"command missing field {required_field!r}: {cmd}"
            )
        assert isinstance(cmd["args"], list), f"args must be list in {cmd}"
        assert isinstance(cmd["options"], list), f"options must be list in {cmd}"

    error_codes = envelope["error_codes"]
    assert isinstance(error_codes, list), "error_codes must be a list"
    codes = {entry.get("code") for entry in error_codes if isinstance(entry, dict)}
    assert EXPECTED_ERROR_CODES.issubset(codes), (
        f"error_codes must cover spec §B2 codes 0-5; missing "
        f"{sorted(EXPECTED_ERROR_CODES - codes)}; got {sorted(codes)}"
    )

    auth = envelope["auth"]
    assert isinstance(auth, dict), f"auth must be a dict, got {type(auth)}"
    auth_missing = REQUIRED_AUTH_KEYS - set(auth.keys())
    assert not auth_missing, f"auth missing keys {sorted(auth_missing)}"
    assert isinstance(auth["required_env_vars"], list), (
        "auth.required_env_vars must be a list"
    )
    assert len(auth["required_env_vars"]) >= 1, (
        "auth.required_env_vars must list at least one env var"
    )


# ---------------------------------------------------------------------------
# Fast tests — import-based, run on every pytest invocation
# ---------------------------------------------------------------------------


class TestAgentContextEnvelope:
    """Generated CLI's ``agent-context`` emits a valid agent-context v1 envelope."""

    def test_agent_context_exit_zero(self, imported_cli) -> None:
        """``<slug> agent-context`` exits 0 (CliRunner-driven)."""
        runner = CliRunner()
        result = runner.invoke(imported_cli, ["agent-context"])
        assert result.exit_code == 0, (
            f"agent-context exited {result.exit_code}; "
            f"stdout={result.stdout!r} exc={result.exception!r}"
        )

    def test_agent_context_json_parses(self, imported_cli) -> None:
        """``agent-context`` stdout is a single parseable JSON object (R2)."""
        runner = CliRunner()
        result = runner.invoke(imported_cli, ["agent-context"])
        assert result.exit_code == 0, result.stdout

        # R2: stdout must be JSON-only, exactly one line.
        stdout = result.stdout.strip()
        assert stdout, "agent-context emitted empty stdout"
        # Should be parseable as a JSON object (not an array, not a literal).
        envelope = json.loads(stdout)
        assert isinstance(envelope, dict), (
            f"agent-context output must be a JSON object, got {type(envelope)}"
        )

    def test_agent_context_matches_v1_schema(self, imported_cli) -> None:
        """Envelope has schema_version=1 + all required top-level keys + B2 codes."""
        runner = CliRunner()
        result = runner.invoke(imported_cli, ["agent-context"])
        assert result.exit_code == 0, result.stdout

        envelope = json.loads(result.stdout.strip())
        _assert_agent_context_v1_envelope(envelope, app_slug=APP_NAME)

    def test_agent_context_advertises_actor_sub_env_var(self, imported_cli) -> None:
        """``auth.required_env_vars`` lists the per-app actor-sub env (Invariant 6)."""
        runner = CliRunner()
        result = runner.invoke(imported_cli, ["agent-context"])
        envelope = json.loads(result.stdout.strip())

        app_upper = APP_NAME.replace("-", "_").upper()
        expected_user_sub = f"{app_upper}_USER_SUB"
        env_vars = set(envelope["auth"]["required_env_vars"])
        assert expected_user_sub in env_vars, (
            f"auth.required_env_vars must include {expected_user_sub} so "
            f"agents know which env to set; got {sorted(env_vars)}"
        )

    def test_agent_context_includes_example_subcommand(self, imported_cli) -> None:
        """Walking the Typer tree picks up mounted sub-typers (example group)."""
        runner = CliRunner()
        result = runner.invoke(imported_cli, ["agent-context"])
        envelope = json.loads(result.stdout.strip())

        command_names = {cmd["name"] for cmd in envelope["commands"]}
        # main.py mounts the `example` sub-typer; agent_context walks one
        # level deep, so we expect at least `example ping` or `example whoami`.
        assert any(name.startswith("example") for name in command_names), (
            f"agent-context must surface mounted sub-typers; got commands "
            f"{sorted(command_names)}"
        )


class TestVersionCommand:
    """Generated CLI's ``version`` command prints a version + exits 0."""

    def test_version_exit_zero(self, imported_cli) -> None:
        """``<slug> version`` exits 0 with a version string on stdout."""
        runner = CliRunner()
        result = runner.invoke(imported_cli, ["version"])
        assert result.exit_code == 0, (
            f"version exited {result.exit_code}; "
            f"stdout={result.stdout!r} exc={result.exception!r}"
        )
        # main.py's `_resolve_version()` falls back to "0.1.0" when
        # app.routes.version is unimportable; either way it must be non-empty.
        assert result.stdout.strip(), "version emitted empty stdout"


# ---------------------------------------------------------------------------
# Slow test — full pip install + subprocess invoke of the installed binary
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestInstalledBinaryEntryPoint:
    """End-to-end: `pip install -e .` then invoke installed ``<slug>`` binary.

    This is the only path that exercises the ``[project.scripts]`` entry
    point in ``pyproject.toml``. The fast tests import ``app.cli.main:app``
    directly and never go through ``pip install``. If the script entry is
    misspelled or the wheel build fails, only this test catches it.

    Slow because ``pip install -e .`` resolves and installs FastAPI +
    pyjwt + cryptography + asyncpg, ~30-60s on a warm pip cache.

    Marked ``@pytest.mark.slow``; ``pytest -m "not slow"`` skips it.
    Pytest emits a warning about the unknown marker but runs the test
    regardless; that warning is intentional (we deliberately don't
    register the marker globally to avoid touching tests/conftest.py
    which a parallel task is editing).
    """

    def test_installed_agent_context_emits_valid_envelope(
        self, tmp_path: Path, rendered_app_dir: Path
    ) -> None:
        """Install the rendered app + invoke ``<slug> agent-context --json``."""
        # Copy rendered tree to a writable location (the module-scoped
        # fixture's path is shared; venv install would mutate it).
        import shutil

        app_dir = tmp_path / APP_NAME
        shutil.copytree(rendered_app_dir, app_dir)

        # Create a fresh venv inside the app dir.
        venv_dir = app_dir / ".venv"
        venv.create(venv_dir, with_pip=True, clear=True)

        pip_bin = venv_dir / "bin" / "pip"
        slug_bin = venv_dir / "bin" / APP_NAME

        # Install the rendered app editably. This is the slow step.
        install_result = subprocess.run(
            [str(pip_bin), "install", "-e", "."],
            capture_output=True,
            text=True,
            cwd=str(app_dir),
            timeout=180,
        )
        assert install_result.returncode == 0, (
            f"pip install -e . failed (exit {install_result.returncode}):\n"
            f"stdout: {install_result.stdout[-2000:]}\n"
            f"stderr: {install_result.stderr[-2000:]}"
        )

        # The console script must exist after install.
        assert slug_bin.exists(), (
            f"expected installed binary at {slug_bin}; "
            f"venv bin contents: {sorted((venv_dir / 'bin').iterdir())}"
        )

        # Invoke `<slug> agent-context` — global --json is off by default
        # but agent-context always emits JSON per spec.
        run_env = {
            **os.environ,
            "LOGTO_ENDPOINT": "https://logto.example.com",
            "LOGTO_API_RESOURCE": f"https://api.{APP_NAME}.example.com",
            "LOGTO_M2M_AGENT_APP_ID": "test-m2m-app-id",
        }
        ctx_result = subprocess.run(
            [str(slug_bin), "agent-context"],
            capture_output=True,
            text=True,
            cwd=str(app_dir),
            timeout=15,
            env=run_env,
        )
        assert ctx_result.returncode == 0, (
            f"`{APP_NAME} agent-context` exited {ctx_result.returncode}\n"
            f"stdout: {ctx_result.stdout}\n"
            f"stderr: {ctx_result.stderr}"
        )

        # R2: stdout is exactly one JSON object.
        envelope = json.loads(ctx_result.stdout.strip())
        _assert_agent_context_v1_envelope(envelope, app_slug=APP_NAME)

        # Sanity: `--version`-style version command also works on installed binary.
        ver_result = subprocess.run(
            [str(slug_bin), "version"],
            capture_output=True,
            text=True,
            cwd=str(app_dir),
            timeout=10,
            env=run_env,
        )
        assert ver_result.returncode == 0, (
            f"`{APP_NAME} version` exited {ver_result.returncode}\n"
            f"stderr: {ver_result.stderr}"
        )
        assert ver_result.stdout.strip(), "installed `version` emitted empty stdout"
