"""Tests for railway.json template rendering."""

import json


class TestRailwayJson:
    """Test railway.json is generated correctly for Railway deployment config."""

    def test_railway_json_exists(self, copier_copy):
        """railway.json is generated at root of app directory."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "railway.json").exists()

    def test_railway_json_valid_json(self, copier_copy):
        """railway.json parses as valid JSON."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "railway.json").read_text()
        data = json.loads(content)
        assert isinstance(data, dict)

    def test_railway_json_dockerfile_builder(self, copier_copy):
        """railway.json specifies DOCKERFILE builder."""
        result_dir = copier_copy(include_frontend=True)
        data = json.loads((result_dir / "railway.json").read_text())
        assert data["build"]["builder"] == "DOCKERFILE"

    def test_railway_json_predeploy_command(self, copier_copy):
        """railway.json preDeployCommand runs alembic migrations (exact match)."""
        result_dir = copier_copy(include_frontend=True)
        data = json.loads((result_dir / "railway.json").read_text())
        assert data["deploy"]["preDeployCommand"] == (
            "alembic -c app/db/migrations/alembic.ini upgrade head"
        )

    def test_railway_json_healthcheck(self, copier_copy):
        """railway.json healthcheck points to /health endpoint."""
        result_dir = copier_copy(include_frontend=True)
        data = json.loads((result_dir / "railway.json").read_text())
        assert data["deploy"]["healthcheckPath"] == "/health"

    def test_railway_json_restart_policy(self, copier_copy):
        """railway.json restart policy is ON_FAILURE with max 3 retries."""
        result_dir = copier_copy(include_frontend=True)
        data = json.loads((result_dir / "railway.json").read_text())
        assert data["deploy"]["restartPolicyType"] == "ON_FAILURE"
        assert data["deploy"]["restartPolicyMaxRetries"] == 3

    def test_railway_json_healthcheck_timeout(self, copier_copy):
        """railway.json healthcheck timeout is 30 seconds."""
        result_dir = copier_copy(include_frontend=True)
        data = json.loads((result_dir / "railway.json").read_text())
        assert data["deploy"]["healthcheckTimeout"] == 30

    def test_railway_json_exists_without_frontend(self, copier_copy):
        """railway.json is generated even without a frontend — it is
        deployment config, not frontend-dependent."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "railway.json").exists()
        data = json.loads((result_dir / "railway.json").read_text())
        assert data["build"]["builder"] == "DOCKERFILE"

    def test_railway_json_exists_for_explicit_railway_target(self, copier_copy):
        """deploy_target=railway renders railway.json and no render.yaml."""
        result_dir = copier_copy(include_frontend=True, deploy_target="railway")
        assert (result_dir / "railway.json").exists()
        assert not (result_dir / "render.yaml").exists()

    def test_railway_json_absent_for_render_target(self, copier_copy):
        """deploy_target=render replaces railway.json with render.yaml."""
        result_dir = copier_copy(include_frontend=True, deploy_target="render")
        assert not (result_dir / "railway.json").exists()
        assert (result_dir / "render.yaml").exists()
