"""Shared test fixtures for agentic-core tests."""

import subprocess
import sys
import re
from pathlib import Path

import pytest


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    """Strip ANSI SGR codes from CLI output before asserting on it.

    Rich colourises Typer's help and progress output when it thinks the
    consumer supports it — which GitHub Actions does, but a local pytest run
    usually does not. With styling on, Rich inserts escape sequences *inside*
    flag names, so `"--no-auth" in result.stdout` is False on CI and True on a
    laptop for identical code. Assert on the stripped text instead.
    """
    return _ANSI_RE.sub("", text)


REPO_ROOT = Path(__file__).parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"
# Use copier from the same venv as the test runner
COPIER_BIN = str(Path(sys.executable).parent / "copier")


@pytest.fixture
def copier_copy(tmp_path):
    """Helper to run copier copy with default variables.

    Returns a function that accepts optional overrides for template variables.
    The function returns the path to the generated app directory (tmp_path / app_name / app_name).
    Copier renders template/{{ app_name }}/ into dst/app_name/, so the actual app
    is at dst / app_name.
    """

    def _copy(
        app_name="test-app",
        app_description="A test application",
        include_frontend=False,
        include_mobile=None,
        github_user="testuser",
        logto_endpoint="https://logto.example.com",
        **extra_vars,
    ):
        dst = tmp_path / "output"
        dst.mkdir(exist_ok=True)

        cmd = [
            COPIER_BIN,
            "copy",
            str(TEMPLATE_DIR),
            str(dst),
            "--trust",
            "--skip-tasks",
            "--defaults",
            "--data", f"app_name={app_name}",
            "--data", f"app_description={app_description}",
            "--data", f"include_frontend={str(include_frontend).lower()}",
            "--data", f"github_user={github_user}",
            "--data", f"logto_endpoint={logto_endpoint}",
        ]
        if include_mobile is not None:
            cmd.extend(["--data", f"include_mobile={str(include_mobile).lower()}"])
        for key, value in extra_vars.items():
            rendered = str(value).lower() if isinstance(value, bool) else value
            cmd.extend(["--data", f"{key}={rendered}"])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise RuntimeError(
                f"copier copy failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout}\n"
                f"stderr: {result.stderr}"
            )
        # Copier renders template/{{ app_name }}/ into dst/app_name/
        return dst / app_name

    return _copy
