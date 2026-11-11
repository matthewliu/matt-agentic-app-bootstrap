"""Generate a Google sign-in app and run its own test suite and linter.

The generated suite plays Google with a local fake provider, so this test
makes no network call apart from `pip install`.
"""

import subprocess
import sys

import pytest


@pytest.fixture(scope="module")
def google_app(tmp_path_factory):
    from tests.conftest import COPIER_BIN, TEMPLATE_DIR

    dst = tmp_path_factory.mktemp("google-integration")
    app_name = "google-integ-app"
    app_dir = dst / app_name
    cmd = [
        COPIER_BIN, "copy", str(TEMPLATE_DIR), str(dst),
        "--trust", "--skip-tasks", "--defaults",
        "--data", f"app_name={app_name}",
        "--data", "auth_provider=google",
        "--data", "deploy_target=render",
        "--data", "include_frontend=false",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"copier copy failed: {result.stderr}"

    venv_dir = app_dir / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, timeout=60)
    result = subprocess.run(
        [str(venv_dir / "bin" / "pip"), "install", "-e", ".[dev]"],
        capture_output=True,
        text=True,
        cwd=str(app_dir),
        timeout=300,
    )
    assert result.returncode == 0, f"pip install failed: {result.stderr}"
    return app_dir


def test_generated_google_app_tests_pass(google_app):
    result = subprocess.run(
        [str(google_app / ".venv" / "bin" / "pytest"), "tests/", "-q", "--tb=short"],
        capture_output=True,
        text=True,
        cwd=str(google_app),
        timeout=180,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    # The sign-in and agent key suites ran, not just the item tests.
    collected = subprocess.run(
        [str(google_app / ".venv" / "bin" / "pytest"), "tests/", "--co", "-q"],
        capture_output=True,
        text=True,
        cwd=str(google_app),
        timeout=60,
    ).stdout
    assert "test_google_auth.py" in collected and "test_agent_keys.py" in collected


def test_generated_google_app_passes_ruff(google_app):
    result = subprocess.run(
        [str(google_app / ".venv" / "bin" / "ruff"), "check", "."],
        capture_output=True,
        text=True,
        cwd=str(google_app),
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
