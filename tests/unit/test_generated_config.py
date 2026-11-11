"""Tests for generated configuration and meta files."""

import importlib
import sys

import pytest
import yaml



class TestPyprojectToml:
    """Validate generated pyproject.toml."""

    def test_pyproject_exists(self, copier_copy):
        """Generated app includes pyproject.toml."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "pyproject.toml").exists()

    def test_pyproject_valid_toml(self, copier_copy):
        """Generated pyproject.toml parses as valid TOML."""
        import tomllib

        result_dir = copier_copy(include_frontend=False)
        with open(result_dir / "pyproject.toml", "rb") as f:
            config = tomllib.load(f)
        assert "project" in config
        assert config["project"]["name"] == "test-app"

    def test_pyproject_has_dependencies(self, copier_copy):
        """Generated pyproject.toml lists required dependencies."""
        import tomllib

        result_dir = copier_copy(include_frontend=False)
        with open(result_dir / "pyproject.toml", "rb") as f:
            config = tomllib.load(f)
        deps = config["project"]["dependencies"]
        dep_names = [d.split(">=")[0].split("[")[0] for d in deps]
        assert "fastapi" in dep_names
        assert "sqlalchemy" in dep_names
        assert "pyjwt" in dep_names
        assert "certifi" in dep_names

    def test_pyproject_has_dev_dependencies(self, copier_copy):
        """Generated pyproject.toml includes dev dependencies."""
        import tomllib

        result_dir = copier_copy(include_frontend=False)
        with open(result_dir / "pyproject.toml", "rb") as f:
            config = tomllib.load(f)
        dev_deps = config["project"]["optional-dependencies"]["dev"]
        dep_names = [d.split(">=")[0] for d in dev_deps]
        assert "pytest" in dep_names
        assert "ruff" in dep_names
        assert "aiosqlite" in dep_names

    def test_pyproject_has_pytest_config(self, copier_copy):
        """Generated pyproject.toml includes pytest configuration."""
        import tomllib

        result_dir = copier_copy(include_frontend=False)
        with open(result_dir / "pyproject.toml", "rb") as f:
            config = tomllib.load(f)
        assert "tool" in config
        assert "pytest" in config["tool"]

    def test_pyproject_has_ruff_config(self, copier_copy):
        """Generated pyproject.toml includes ruff configuration."""
        import tomllib

        result_dir = copier_copy(include_frontend=False)
        with open(result_dir / "pyproject.toml", "rb") as f:
            config = tomllib.load(f)
        assert "ruff" in config["tool"]

    def test_pyproject_app_name_substituted(self, copier_copy):
        """App name is correctly substituted in pyproject.toml."""
        import tomllib

        result_dir = copier_copy(app_name="my-cool-app", include_frontend=False)
        with open(result_dir / "pyproject.toml", "rb") as f:
            config = tomllib.load(f)
        assert config["project"]["name"] == "my-cool-app"


class TestGitignore:
    """Validate generated .gitignore."""

    def test_gitignore_exists(self, copier_copy):
        """Generated app includes .gitignore."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / ".gitignore").exists()

    def test_gitignore_covers_sensitive_files(self, copier_copy):
        """Generated .gitignore includes .env, .venv, __pycache__."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / ".gitignore").read_text()
        assert ".env" in content
        assert ".venv/" in content
        assert "__pycache__/" in content
        assert ".pytest_cache/" in content

    def test_gitignore_covers_frontend(self, copier_copy):
        """Generated .gitignore includes frontend build artifacts."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / ".gitignore").read_text()
        assert "frontend/node_modules/" in content
        assert "frontend/dist/" in content


class TestDockerfile:
    """Validate generated Dockerfile."""

    def test_dockerfile_exists(self, copier_copy):
        """Generated app includes Dockerfile."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "Dockerfile").exists()

    def test_dockerfile_has_python_base(self, copier_copy):
        """Dockerfile uses python:3.12-slim base image."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "Dockerfile").read_text()
        assert "python:3.12-slim" in content

    def test_dockerfile_has_uvicorn_cmd(self, copier_copy):
        """Dockerfile CMD runs uvicorn with shell-form for $PORT substitution."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "Dockerfile").read_text()
        assert "uvicorn app.main:app" in content
        assert "${PORT:-8080}" in content

    def test_dockerfile_no_frontend_skips_node(self, copier_copy):
        """Without frontend, Dockerfile has no Node.js build stage."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "Dockerfile").read_text()
        assert "node:" not in content
        assert "npm ci" not in content
        assert "VITE_" not in content

    def test_dockerfile_with_frontend_has_multistage(self, copier_copy):
        """With frontend, Dockerfile uses multi-stage build with Node."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "Dockerfile").read_text()
        assert "node:" in content
        assert "frontend-build" in content
        assert "pnpm install --no-frozen-lockfile" in content
        assert "ARG VITE_LOGTO_ENDPOINT" in content
        assert "ARG VITE_LOGTO_APP_ID" in content
        assert "ARG VITE_LOGTO_API_RESOURCE" in content
        assert "COPY --from=frontend-build" in content

    def test_dockerignore_excludes_host_artifacts_and_env(self, copier_copy):
        """A local `pnpm install` or venv must never reach the image build context.

        Without this, `COPY frontend/ frontend/` copies the host's
        node_modules over the container's and `tsc` fails, and a local
        `.env` with credentials would sit in the build context.
        """
        result_dir = copier_copy(include_frontend=True)
        lines = (result_dir / ".dockerignore").read_text().splitlines()
        for pattern in ("**/node_modules", "**/dist", ".venv", ".env", ".git"):
            assert pattern in lines, f".dockerignore must exclude {pattern}"
        assert "!.env.example" in lines

    def test_railway_json_exists(self, copier_copy):
        """railway.json is generated for Railway config-as-code deployment."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "railway.json").exists()


class TestEnvExample:
    """Validate generated .env.example."""

    def test_env_example_exists(self, copier_copy):
        """Generated app includes .env.example."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / ".env.example").exists()

    def test_env_example_has_required_vars(self, copier_copy):
        """Generated .env.example documents all required env vars."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / ".env.example").read_text()
        assert "DATABASE_URL" in content
        assert "LOGTO_ENDPOINT" in content
        assert "LOGTO_APP_ID" in content
        assert "LOGTO_APP_SECRET" in content
        assert "LOGTO_API_RESOURCE" in content
        assert "CORS_ORIGINS" in content


class TestCiYml:
    """Validate generated GitHub Actions CI."""

    def test_ci_yml_exists(self, copier_copy):
        """Generated app includes CI workflow."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / ".github" / "workflows" / "ci.yml").exists()

    def test_ci_yml_valid_yaml(self, copier_copy):
        """Generated ci.yml parses as valid YAML."""
        result_dir = copier_copy(include_frontend=False)
        with open(result_dir / ".github" / "workflows" / "ci.yml") as f:
            config = yaml.safe_load(f)
        assert "jobs" in config
        assert "backend" in config["jobs"]

    def test_ci_yml_has_pytest(self, copier_copy):
        """CI workflow includes pytest step."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / ".github" / "workflows" / "ci.yml").read_text()
        assert "pytest" in content

    def test_ci_yml_has_ruff(self, copier_copy):
        """CI workflow includes ruff linting step."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / ".github" / "workflows" / "ci.yml").read_text()
        assert "ruff check" in content

    def test_ci_yml_no_frontend_when_disabled(self, copier_copy):
        """CI yml has no frontend job when include_frontend=false."""
        result_dir = copier_copy(include_frontend=False)
        with open(result_dir / ".github" / "workflows" / "ci.yml") as f:
            config = yaml.safe_load(f)
        assert "frontend" not in config["jobs"]

    def test_ci_yml_has_frontend_when_enabled(self, copier_copy):
        """CI yml includes frontend job when include_frontend=true."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / ".github" / "workflows" / "ci.yml") as f:
            config = yaml.safe_load(f)
        assert "frontend" in config["jobs"]

    def test_ci_yml_frontend_has_npm_test(self, copier_copy):
        """Frontend CI job includes npm test and npm run build."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / ".github" / "workflows" / "ci.yml").read_text()
        assert "npm test" in content
        assert "npm run build" in content


class TestCopierAnswers:
    """Validate generated .copier-answers.yml."""

    def test_copier_answers_exists(self, copier_copy):
        """Generated app includes .copier-answers.yml."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / ".copier-answers.yml").exists()

    def test_copier_answers_has_variables(self, copier_copy):
        """Generated .copier-answers.yml records template variables."""
        result_dir = copier_copy(app_name="my-app", include_frontend=False)
        with open(result_dir / ".copier-answers.yml") as f:
            answers = yaml.safe_load(f)
        assert answers["app_name"] == "my-app"
        assert "include_frontend" in answers


class TestCorsOrigins:
    """KTD6: on Render, CORS derives from RENDER_EXTERNAL_URL unless CORS_ORIGINS is set."""

    LOCALHOST_DEFAULTS = ["http://localhost:3000", "http://localhost:5173"]

    @pytest.fixture
    def settings_cls(self, copier_copy, tmp_path, monkeypatch):
        """Import the rendered app's Settings with no env file and a clean CORS env."""
        result_dir = copier_copy(include_frontend=False)
        # Point HOME and cwd at an empty dir so neither env file is read.
        empty = tmp_path / "empty-home"
        empty.mkdir()
        monkeypatch.setenv("HOME", str(empty))
        monkeypatch.chdir(empty)
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
        monkeypatch.syspath_prepend(str(result_dir))
        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]
        yield importlib.import_module("app.config").Settings
        for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
            del sys.modules[name]

    def test_render_url_only_when_cors_origins_unset(self, settings_cls, monkeypatch):
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
        assert settings_cls().cors_origins_list == ["https://x.onrender.com"]

    def test_explicit_cors_origins_wins(self, settings_cls, monkeypatch):
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
        monkeypatch.setenv("CORS_ORIGINS", "https://a.example.com,https://b.example.com")
        assert settings_cls().cors_origins_list == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_cors_origins_from_env_file_wins(self, settings_cls, monkeypatch, tmp_path):
        """"Set" includes env files, not only process env vars."""
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
        env_file = tmp_path / "app.env"
        env_file.write_text("CORS_ORIGINS=https://from-file.example.com\n")
        assert settings_cls(_env_file=str(env_file)).cors_origins_list == [
            "https://from-file.example.com"
        ]

    def test_neither_set_keeps_localhost_defaults(self, settings_cls):
        assert settings_cls().cors_origins_list == self.LOCALHOST_DEFAULTS
