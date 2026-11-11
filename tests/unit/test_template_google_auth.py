"""The `auth_provider` answer: Google sign-in for new apps, Logto as legacy.

Rendering checks only. tests/integration/test_generated_google_app.py runs a
generated Google app's own test suite.
"""

import json
import subprocess

import pytest
import yaml

from tests.conftest import COPIER_BIN, TEMPLATE_DIR

GOOGLE_ONLY = [
    "app/auth/agent_keys.py",
    "app/routes/auth.py",
    "tests/google_fake.py",
    "tests/integration/test_google_auth.py",
    "tests/integration/test_agent_keys.py",
    "frontend/src/auth.tsx",
    "frontend/src/__tests__/LoginPage.test.tsx",
]
LOGTO_ONLY = [
    "tests/integration/test_auth_not_configured.py",
    "tests/unit/test_auth_middleware.py",
    "frontend/src/config.ts",
    "frontend/src/pages/CallbackPage.tsx",
    "frontend/.env.example",
]


@pytest.fixture
def google_app(copier_copy):
    return copier_copy(
        app_name="gapp",
        include_frontend=True,
        include_mobile=False,
        auth_provider="google",
        deploy_target="render",
    )


@pytest.fixture
def logto_app(copier_copy):
    return copier_copy(app_name="lapp", include_frontend=True)


class TestGoogleRender:
    def test_google_files_exist_and_logto_files_do_not(self, google_app):
        for path in GOOGLE_ONLY:
            assert (google_app / path).is_file(), path
        for path in LOGTO_ONLY:
            assert not (google_app / path).exists(), path
        assert not (google_app / "mobile").exists()

    def test_no_logto_code_or_dependency(self, google_app):
        middleware = (google_app / "app/auth/middleware.py").read_text()
        assert "logto_" not in middleware and "jwks" not in middleware.lower()
        pkg = json.loads((google_app / "frontend/package.json").read_text())
        assert "@logto/react" not in pkg["dependencies"]
        dockerfile = (google_app / "Dockerfile").read_text()
        assert "VITE_LOGTO" not in dockerfile
        config = (google_app / "app/config.py").read_text()
        assert "logto_" not in config

    def test_backend_depends_on_authlib(self, google_app):
        pyproject = (google_app / "pyproject.toml").read_text()
        assert '"authlib>=1.9.5,<1.10"' in pyproject
        assert '"itsdangerous>=2.1"' in pyproject

    def test_google_settings_never_load_from_the_shared_env_file(self, google_app):
        config = (google_app / "app/config.py").read_text()
        auth_part = config.split("class AuthSettings", 1)[1]
        assert '"env_file": ".env"' in auth_part
        assert "_SHARED_ENV" not in auth_part

    def test_least_scopes_and_pkce(self, google_app):
        routes = (google_app / "app/routes/auth.py").read_text()
        assert 'GOOGLE_SCOPES = "openid email"' in routes
        assert '"code_challenge_method": "S256"' in routes

    def test_agent_key_files_are_git_ignored(self, google_app):
        assert "*.agent-key" in (google_app / ".gitignore").read_text()

    def test_blueprint_declares_google_settings_without_values(self, google_app):
        blueprint = yaml.safe_load((google_app / "render.yaml").read_text())
        env = {v["key"]: v for v in blueprint["services"][0]["envVars"]}
        for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "AUTH_ALLOWED_EMAILS",
                    "AGENT_CREDENTIALS"):
            assert env[key] == {"key": key, "sync": False}
        assert env["SESSION_SECRET"] == {"key": "SESSION_SECRET", "generateValue": True}
        assert env["DATABASE_URL"]["fromDatabase"]["property"] == "connectionString"

    def test_env_example_has_no_logto_and_no_values(self, google_app):
        text = (google_app / ".env.example").read_text()
        assert "LOGTO_" not in text
        for line in text.splitlines():
            if line.startswith(("GOOGLE_CLIENT_SECRET=", "SESSION_SECRET=", "AGENT_CREDENTIALS=")):
                assert line.endswith("="), line


class TestLogtoStaysLegacy:
    def test_default_answer_is_logto(self, logto_app):
        middleware = (logto_app / "app/auth/middleware.py").read_text()
        assert "Logto" in middleware
        for path in LOGTO_ONLY:
            assert (logto_app / path).exists(), path
        for path in GOOGLE_ONLY:
            assert not (logto_app / path).exists(), path
        pkg = json.loads((logto_app / "frontend/package.json").read_text())
        assert "@logto/react" in pkg["dependencies"]
        assert (logto_app / "mobile").is_dir()

    def test_logto_render_app_keeps_the_503_lockout(self, copier_copy):
        app = copier_copy(app_name="lrender", deploy_target="render")
        assert (app / "tests/integration/test_auth_not_configured.py").is_file()
        assert "GOOGLE_CLIENT_ID" not in (app / "render.yaml").read_text()


def test_google_rejects_a_mobile_app(tmp_path):
    result = subprocess.run(
        [
            COPIER_BIN, "copy", str(TEMPLATE_DIR), str(tmp_path / "out"),
            "--trust", "--skip-tasks", "--defaults",
            "--data", "app_name=gmobile",
            "--data", "auth_provider=google",
            "--data", "include_frontend=true",
            "--data", "include_mobile=true",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "not supported with Google sign-in" in result.stdout + result.stderr


def test_google_default_has_no_mobile(copier_copy):
    app = copier_copy(app_name="gdefault", include_frontend=True, auth_provider="google")
    assert not (app / "mobile").exists()


# ---------------------------------------------------------------------------
# Docs of the default (Render + Google) app
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", ["README.md", "CLAUDE.md", "AGENTS.md", ".env.example"])
def test_google_render_docs_never_mention_railway_or_logto_setup(google_app, doc):
    content = (google_app / doc).read_text()
    assert "railway" not in content.lower(), doc
    assert "LOGTO_APP_ID" not in content and "VITE_LOGTO" not in content, doc


def test_google_readme_points_to_machine_credential_and_two_step_revocation(google_app):
    readme = (google_app / "README.md").read_text()
    assert "Check the CLI section" not in readme
    # The CLI and MCP client send agent keys (PR #11). The stale limit must not return.
    assert "do not send agent keys yet" not in readme
    assert "### Machine credential" in readme
    assert 'See "Machine credential"' in readme
    assert "remove every `AGENT_CREDENTIALS` entry" in readme


# ---------------------------------------------------------------------------
# app/config.py: no Logto text and no extra blank lines in Google apps (D5)
# ---------------------------------------------------------------------------


def test_google_config_has_no_logto_text_and_one_blank_line(google_app):
    config = (google_app / "app" / "config.py").read_text()
    assert "logto" not in config.lower()
    assert "Google sign-in and\n# agent key settings never load from it" in config
    # Exactly one blank line between the database setting and the CORS block.
    assert '"postgresql+asyncpg://localhost:5432/gapp"\n\n    # CORS\n' in config


def test_logto_config_keeps_its_comment_and_spacing(logto_app):
    """The Logto render must stay byte-identical; pin the two lines D5 touched."""
    config = (logto_app / "app" / "config.py").read_text()
    comment = "# Shared platform credentials (Logto M2M, Railway token) live here"
    assert f"{comment}\n_SHARED_ENV" in config
    assert ':5432/lapp"\n\n    # Logto auth\n' in config
    assert '    logto_native_app_id: str = ""\n\n    # CORS\n' in config
