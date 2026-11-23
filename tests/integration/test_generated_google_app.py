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


def test_generated_settings_ignore_a_shared_home_env_file(google_app, tmp_path):
    """A ``~/.agentic-apps.env`` must not reach a generated app.

    The old shared file let one ``CORS_ORIGINS`` or ``DATABASE_URL`` value
    configure every app on the machine. The run starts from an empty
    environment with only ``HOME`` and ``PATH`` set, plus the Render URL.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".agentic-apps.env").write_text(
        "CORS_ORIGINS=https://attacker.example\n"
        "DATABASE_URL=postgresql://shared@shared-host/shared\n"
    )
    code = (
        "from app.config import settings; "
        "print(settings.cors_origins_list); print(settings.database_url)"
    )
    result = subprocess.run(
        [str(google_app / ".venv" / "bin" / "python"), "-c", code],
        cwd=str(google_app), capture_output=True, text=True, timeout=60,
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            "RENDER_EXTERNAL_URL": "https://gapp.onrender.com",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "attacker.example" not in result.stdout
    assert "shared-host" not in result.stdout
    assert "['https://gapp.onrender.com']" in result.stdout
    assert "postgresql+asyncpg://localhost:5432/google_integ_app" in result.stdout
