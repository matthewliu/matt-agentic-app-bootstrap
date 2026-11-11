"""Smoke test to verify test infrastructure works."""

from pathlib import Path


def test_repo_root_exists():
    """Verify the repo root is accessible from tests."""
    from tests.conftest import REPO_ROOT
    assert REPO_ROOT.exists()
    assert (REPO_ROOT / "pyproject.toml").exists()


def test_template_dir_defined():
    """Verify template directory constant is set (template may not exist yet)."""
    from tests.conftest import TEMPLATE_DIR
    assert TEMPLATE_DIR == REPO_ROOT / "templates" / "mini-app"


REPO_ROOT = Path(__file__).parent.parent.parent
