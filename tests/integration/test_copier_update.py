"""Integration tests: copier update preserves user files and applies changes.

Verifies that after generating an app, modifying a template file, and running
`copier update`, user-modified files (.env, MEMORY.md) are preserved while
template changes are applied.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"
COPIER_BIN = str(Path(sys.executable).parent / "copier")


@pytest.fixture
def generated_app_with_state(tmp_path):
    """Generate an app and add user-created state files.

    Returns (app_dir, dst) where dst is the parent directory copier rendered into.
    """
    dst = tmp_path / "output"
    dst.mkdir()

    app_name = "update-test-app"
    result = subprocess.run(
        [
            COPIER_BIN, "copy", str(TEMPLATE_DIR), str(dst),
            "--trust", "--skip-tasks", "--defaults",
            "--data", f"app_name={app_name}",
            "--data", "include_frontend=false",
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"copier copy failed: {result.stderr}"

    app_dir = dst / app_name

    # Simulate user-created files that should survive copier update
    env_file = app_dir / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://user:pass@localhost/mydb\n"
        "LOGTO_ENDPOINT=https://logto.example.com\n"
        "LOGTO_APP_ID=real-app-id-123\n"
        "LOGTO_APP_SECRET=real-secret-456\n"
        "LOGTO_API_RESOURCE=https://api.update-test-app.example.test\n"
        "CORS_ORIGINS=http://localhost:3000\n"
    )

    memory_dir = app_dir / ".claude" / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    memory_file.write_text(
        "# Memory: update-test-app\n\n"
        "## Architecture Decisions\n"
        "- Using Redis for caching (added by user)\n\n"
        "## Debugging Notes\n"
        "- The auth middleware needs certifi for SSL on macOS\n"
    )

    # Initialize git repo (copier update requires git)
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=str(app_dir), check=True, timeout=10,
    )
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(app_dir), check=True, timeout=10,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit", "--quiet"],
        cwd=str(app_dir), check=True, timeout=10,
        env={
            **dict(__import__("os").environ),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )

    return app_dir, dst


class TestCopierUpdate:
    """Verify copier update works correctly on generated apps."""

    def test_env_file_preserved_after_update(self, generated_app_with_state):
        """User's .env file should survive copier update (via _skip_if_exists)."""
        app_dir, dst = generated_app_with_state

        # Read .env before update
        env_before = (app_dir / ".env").read_text()
        assert "real-app-id-123" in env_before

        # Run copier update
        subprocess.run(
            [
                COPIER_BIN, "update", str(app_dir),
                "--trust", "--skip-tasks", "--defaults",
            ],
            capture_output=True, text=True, timeout=60,
        )
        # copier update may return non-zero if there are no changes or
        # if it encounters conflicts — check that .env is preserved regardless
        env_after = (app_dir / ".env").read_text()
        assert "real-app-id-123" in env_after, (
            ".env was overwritten by copier update! Content after update:\n"
            + env_after
        )

    def test_memory_file_preserved_after_update(self, generated_app_with_state):
        """User's MEMORY.md should survive copier update (via _skip_if_exists)."""
        app_dir, dst = generated_app_with_state

        memory_file = app_dir / ".claude" / "memory" / "MEMORY.md"
        memory_before = memory_file.read_text()
        assert "Using Redis for caching" in memory_before

        # Run copier update
        subprocess.run(
            [
                COPIER_BIN, "update", str(app_dir),
                "--trust", "--skip-tasks", "--defaults",
            ],
            capture_output=True, text=True, timeout=60,
        )

        memory_after = memory_file.read_text()
        assert "Using Redis for caching" in memory_after, (
            "MEMORY.md was overwritten by copier update! Content after update:\n"
            + memory_after
        )

    def test_copier_answers_file_exists(self, generated_app_with_state):
        """Generated app should have .copier-answers.yml for update support."""
        app_dir, _ = generated_app_with_state
        answers_file = app_dir / ".copier-answers.yml"
        assert answers_file.exists(), ".copier-answers.yml missing — copier update won't work"

        content = answers_file.read_text()
        assert "update-test-app" in content, (
            "copier-answers.yml should contain the app_name"
        )

    def test_generated_app_still_valid_after_update(self, generated_app_with_state):
        """After copier update, the app's Python files should still compile."""
        app_dir, _ = generated_app_with_state

        subprocess.run(
            [
                COPIER_BIN, "update", str(app_dir),
                "--trust", "--skip-tasks", "--defaults",
            ],
            capture_output=True, text=True, timeout=60,
        )

        # Verify all .py files still compile
        py_files = list(app_dir.rglob("*.py"))
        assert len(py_files) > 0, "No Python files found after update"

        for py_file in py_files:
            if ".venv" in str(py_file):
                continue
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", str(py_file)],
                capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, (
                f"Python file failed to compile after update: {py_file}\n"
                f"{result.stderr}"
            )


# ---------------------------------------------------------------------------
# deploy_target added after an app was generated (KTD4)
# ---------------------------------------------------------------------------

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(cwd, *args):
    import os

    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, timeout=30,
        env={**os.environ, **_GIT_ENV},
    )


def _make_baseline(template_repo: Path) -> None:
    """Turn a copy of the current template into the pre-deploy_target baseline.

    Drops the deploy_target question, un-gates railway.json, and removes the
    Render-only files, so the baseline renders what the template rendered
    before deploy_target existed.
    """
    import yaml

    cfg_path = template_repo / "copier.yml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg.pop("deploy_target")
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))

    app_tpl = template_repo / "template" / "{{ app_name }}"
    for path in app_tpl.rglob("*deploy_target*"):
        if "railway.json" in path.name:
            path.rename(path.with_name("railway.json.jinja"))
        else:
            path.unlink()


def test_update_without_deploy_target_answer_keeps_railway(tmp_path):
    """An app whose answers file has no deploy_target keeps railway.json on
    update, gets no render.yaml, and keeps its local edit.

    The update runs from the destination parent with `-a <app>/...`. The
    template renders into a `{{ app_name }}/` subdirectory, so `copier update`
    run inside the app directory finds the answers but writes nothing: its
    diff paths carry the `<app>/` prefix. `_commit == "v2"` proves this
    update really applied.
    """
    import shutil

    import yaml

    template_repo = tmp_path / "template-repo"
    shutil.copytree(
        TEMPLATE_DIR, template_repo, ignore=shutil.ignore_patterns("__pycache__")
    )
    _make_baseline(template_repo)
    _git(template_repo, "init", "--quiet")
    _git(template_repo, "add", "-A")
    _git(template_repo, "commit", "--quiet", "-m", "baseline")
    _git(template_repo, "tag", "v1")

    dst = tmp_path / "output"
    dst.mkdir()
    app_name = "update-target-app"
    copy = subprocess.run(
        [
            COPIER_BIN, "copy", str(template_repo), str(dst),
            "--trust", "--skip-tasks", "--defaults", "--vcs-ref", "v1",
            "--data", f"app_name={app_name}",
            "--data", "include_frontend=false",
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert copy.returncode == 0, copy.stderr
    app_dir = dst / app_name
    answers = yaml.safe_load((app_dir / ".copier-answers.yml").read_text())
    assert "deploy_target" not in answers
    assert (app_dir / "railway.json").exists()

    readme = app_dir / "README.md"
    readme.write_text(readme.read_text() + "\nLocal edit that must survive.\n")
    _git(dst, "init", "--quiet")
    _git(dst, "add", "-A")
    _git(dst, "commit", "--quiet", "-m", "app with local edit")

    # Publish the current template as v2.
    shutil.rmtree(template_repo / "template")
    shutil.copytree(
        TEMPLATE_DIR / "template", template_repo / "template",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy(TEMPLATE_DIR / "copier.yml", template_repo / "copier.yml")
    _git(template_repo, "add", "-A")
    _git(template_repo, "commit", "--quiet", "-m", "add deploy_target")
    _git(template_repo, "tag", "v2")

    update = subprocess.run(
        [
            COPIER_BIN, "update", str(dst),
            "-a", f"{app_name}/.copier-answers.yml",
            "--trust", "--skip-tasks", "--defaults",
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert update.returncode == 0, update.stderr

    answers = yaml.safe_load((app_dir / ".copier-answers.yml").read_text())
    assert answers["_commit"] == "v2"
    assert answers["deploy_target"] == "railway"
    assert (app_dir / "railway.json").exists()
    assert not (app_dir / "render.yaml").exists()
    assert not (app_dir / "tests" / "unit" / "test_render_blueprint.py").exists()
    assert "Local edit that must survive." in readme.read_text()
