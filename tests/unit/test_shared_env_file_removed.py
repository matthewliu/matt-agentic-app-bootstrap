"""No part of agentic-core or a generated app reads ``~/.agentic-apps.env``.

That file was shared by every app on the machine, so one value in it reached
every app. A ``CORS_ORIGINS`` or ``DATABASE_URL`` there overrode each app's own
default. The CLI and generated apps now read only their own ``.env`` and the
process environment.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_core import cli

SHARED_NAME = ".agentic-apps.env"
REPO_ROOT = Path(__file__).parents[2]


def _unset(monkeypatch, *names):
    """Unset names so that monkeypatch restores "unset" even if a test fails."""
    for name in names:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)


@pytest.mark.parametrize(
    "shape",
    [
        {"auth_provider": "google", "deploy_target": "render", "include_frontend": True},
        {"auth_provider": "google", "deploy_target": "railway", "include_frontend": False},
        {"auth_provider": "logto", "deploy_target": "render", "include_frontend": True},
        {"auth_provider": "logto", "deploy_target": "railway", "include_frontend": True},
    ],
    ids=["google-render", "google-railway", "logto-render", "logto-railway"],
)
def test_generated_app_never_names_the_shared_env_file(copier_copy, shape):
    app_dir = copier_copy(**shape)
    for path in app_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        assert "agentic-apps.env" not in text, path


@pytest.mark.parametrize("auth_provider", ["google", "logto"])
def test_every_settings_class_reads_only_dot_env(copier_copy, auth_provider):
    config = (copier_copy(auth_provider=auth_provider) / "app" / "config.py").read_text()
    env_files = [line.strip() for line in config.splitlines() if '"env_file"' in line]
    # One env_file per settings class, so a class without one cannot slip through.
    assert env_files
    assert len(env_files) == config.count("(BaseSettings):"), env_files
    assert all(line == '"env_file": ".env",' for line in env_files), env_files
    assert "Path.home()" not in config


def test_cli_loads_dot_env_but_not_the_home_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    (home / SHARED_NAME).write_text("AGC_TEST_FROM_HOME=leaked\n")
    (work / ".env").write_text("AGC_TEST_FROM_DOTENV=loaded\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    _unset(monkeypatch, "AGC_TEST_FROM_HOME", "AGC_TEST_FROM_DOTENV")

    cli._load_env_files()

    assert os.environ.get("AGC_TEST_FROM_DOTENV") == "loaded"
    assert "AGC_TEST_FROM_HOME" not in os.environ


def test_cli_dot_env_never_overrides_an_exported_value(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("AGC_TEST_EXPORTED=from-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGC_TEST_EXPORTED", "from-shell")

    cli._load_env_files()

    assert os.environ["AGC_TEST_EXPORTED"] == "from-shell"


def test_verify_logto_scripts_load_only_the_repo_dot_env(tmp_path):
    """scripts/verify-logto/utils.py reads the repo .env, never a home file.

    The module exits at import when a required value is missing, so it runs in
    a subprocess, from a copy whose repo root is a temp folder.
    """
    repo = tmp_path / "repo"
    scripts = repo / "scripts" / "verify-logto"
    scripts.mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "verify-logto" / "utils.py", scripts / "utils.py")
    required = ("LOGTO_ENDPOINT", "LOGTO_M2M_APP_ID", "LOGTO_M2M_APP_SECRET", "LOGTO_API_RESOURCE")
    (repo / ".env").write_text("".join(f"{name}=from-repo\n" for name in required))
    home = tmp_path / "home"
    home.mkdir()
    (home / SHARED_NAME).write_text("AGC_HOME_SENTINEL=leaked\n")
    shared = tmp_path / "shared.env"
    shared.write_text("AGC_SHARED_SENTINEL=leaked\n")

    code = (
        "import os, utils; "
        "print(os.environ.get('LOGTO_M2M_APP_ID')); "
        "print(os.environ.get('AGC_HOME_SENTINEL')); "
        "print(os.environ.get('AGC_SHARED_SENTINEL'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(scripts), capture_output=True, text=True, timeout=60,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "SHARED_ENV_FILE": str(shared),
        },
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert result.stdout.splitlines()[:3] == ["from-repo", "None", "None"]
