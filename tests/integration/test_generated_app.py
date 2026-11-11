"""Integration tests — generate an app and verify its test suite passes.

These tests validate the "tests-testing-tests" loop: the template generates
an app with its own test suite, and we verify that test suite passes.
"""

import subprocess
import sys

import pytest


@pytest.fixture(scope="module")
def generated_app(tmp_path_factory):
    """Generate a test app once for all tests in this module.

    This is module-scoped to avoid regenerating + installing for each test.
    """
    from tests.conftest import COPIER_BIN, TEMPLATE_DIR

    dst = tmp_path_factory.mktemp("integration")
    app_name = "integ-test-app"
    app_dir = dst / app_name

    # Generate app with copier
    cmd = [
        COPIER_BIN, "copy", str(TEMPLATE_DIR), str(dst),
        "--trust", "--skip-tasks", "--defaults",
        "--data", f"app_name={app_name}",
        "--data", "app_description=Integration test application",
        "--data", "include_frontend=false",
        "--data", "github_user=testuser",
        "--data", "logto_endpoint=https://logto.example.com",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"copier copy failed: {result.stderr}"

    # Create venv and install deps
    venv_dir = app_dir / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        timeout=30,
    )
    pip_bin = str(venv_dir / "bin" / "pip")
    result = subprocess.run(
        [pip_bin, "install", "-e", ".[dev]"],
        capture_output=True,
        text=True,
        cwd=str(app_dir),
        timeout=120,
    )
    assert result.returncode == 0, f"pip install failed: {result.stderr}"

    return app_dir


class TestGeneratedBackendTests:
    """Verify the generated app's pytest suite passes."""

    def test_generated_backend_tests_pass(self, generated_app):
        """Run pytest in the generated app — all tests should pass."""
        pytest_bin = str(generated_app / ".venv" / "bin" / "pytest")
        result = subprocess.run(
            [pytest_bin, "tests/", "-v", "--tb=short"],
            capture_output=True,
            text=True,
            cwd=str(generated_app),
            timeout=60,
        )
        assert result.returncode == 0, (
            f"Generated app tests failed:\n{result.stdout}\n{result.stderr}"
        )

    def test_generated_app_has_expected_test_count(self, generated_app):
        """Generated app should have at least 15 tests."""
        pytest_bin = str(generated_app / ".venv" / "bin" / "pytest")
        result = subprocess.run(
            [pytest_bin, "tests/", "--co", "-q"],
            capture_output=True,
            text=True,
            cwd=str(generated_app),
            timeout=30,
        )
        # Count lines that look like test items
        test_lines = [
            line for line in result.stdout.strip().split("\n")
            if "::" in line
        ]
        assert len(test_lines) >= 15, (
            f"Expected >= 15 tests, found {len(test_lines)}"
        )

    def test_ruff_check_passes(self, generated_app):
        """Generated app passes ruff linting."""
        ruff_bin = str(generated_app / ".venv" / "bin" / "ruff")
        result = subprocess.run(
            [ruff_bin, "check", "."],
            capture_output=True,
            text=True,
            cwd=str(generated_app),
            timeout=30,
        )
        assert result.returncode == 0, (
            f"ruff check failed:\n{result.stdout}\n{result.stderr}"
        )
