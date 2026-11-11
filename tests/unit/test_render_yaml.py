"""Tests for the Render Blueprint (render.yaml) and the CA-advisory Docker patch.

`deploy_target=render` must produce a `render.yaml` and no `railway.json`
(KTD1, KTD4). The Blueprint shape is pinned by KTD3 (plans, region, disk,
Postgres version, pre-deploy command) and KTD5 (DATABASE_URL is the only env
var). The Dockerfile and `pyproject.toml` checks pin KTD10.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agentic_core.bootstrap import template as template_mod
from tests.conftest import TEMPLATE_DIR

SLUG = "render-app"
PREDEPLOY = f"{SLUG}-migrate"
GUARD_TEST = Path("tests") / "unit" / "test_render_blueprint.py"


@pytest.fixture
def render_full(copier_copy):
    """`full` variant, frontend on, database on, deploy target Render."""
    return copier_copy(
        app_name=SLUG,
        include_frontend=True,
        include_mobile=False,
        deploy_target="render",
    )


def _blueprint(app_dir: Path) -> dict:
    return yaml.safe_load((app_dir / "render.yaml").read_text())


def _web(bp: dict) -> dict:
    services = bp["services"]
    assert len(services) == 1, f"expected one service, got {services}"
    assert services[0]["type"] == "web"
    return services[0]


class TestRenderBlueprintFull:
    def test_one_web_service_and_one_database_no_railway_json(self, render_full):
        bp = _blueprint(render_full)
        assert _web(bp)["name"] == SLUG
        assert [db["name"] for db in bp["databases"]] == [f"{SLUG}-db"]
        assert not (render_full / "railway.json").exists()

    def test_web_service_shape(self, render_full):
        web = _web(_blueprint(render_full))
        assert web["runtime"] == "docker"
        assert web["plan"] == "0.5c-512mb"
        assert web["healthCheckPath"] == "/health"
        assert web["autoDeployTrigger"] == "checksPass"

    def test_database_is_private_and_sized(self, render_full):
        bp = _blueprint(render_full)
        (db,) = bp["databases"]
        assert db["ipAllowList"] == []
        assert db["diskSizeGB"] == 1
        assert db["postgresMajorVersion"] == "16"
        assert db["plan"] == "0.1c-256mb"
        assert db["region"] == "oregon"
        assert _web(bp)["region"] == "oregon"

    def test_only_env_var_is_database_url_from_database(self, render_full):
        env_vars = _web(_blueprint(render_full))["envVars"]
        assert env_vars == [
            {
                "key": "DATABASE_URL",
                "fromDatabase": {"name": f"{SLUG}-db", "property": "connectionString"},
            }
        ]
        keys = [v["key"] for v in env_vars]
        assert not [k for k in keys if k.startswith(("LOGTO_", "VITE_"))]

    def test_predeploy_takes_no_arguments(self, render_full):
        # Render runs a Docker pre-deploy command as `/bin/sh -c <words>`:
        # only the first word runs, so the command must be a single word.
        cmd = _web(_blueprint(render_full))["preDeployCommand"]
        assert cmd == PREDEPLOY
        assert " " not in cmd

    def test_migrate_entry_point_is_installed(self, render_full):
        pyproject = (render_full / "pyproject.toml").read_text()
        assert f'{PREDEPLOY} = "app.db.migrate:main"' in pyproject
        assert (render_full / "app" / "db" / "migrate.py").is_file()


class TestRenderBlueprintNoDb:
    def test_no_database_no_predeploy_no_env_vars(self, copier_copy):
        app_dir = copier_copy(
            app_name=SLUG, include_frontend=True, include_mobile=False,
            deploy_target="render", with_db=False,
        )
        bp = _blueprint(app_dir)
        assert "databases" not in bp
        web = _web(bp)
        assert "preDeployCommand" not in web
        assert "envVars" not in web


class TestDefaultTarget:
    def test_default_renders_railway_json_only(self, copier_copy):
        app_dir = copier_copy(include_frontend=True)
        assert (app_dir / "railway.json").exists()
        assert not (app_dir / "render.yaml").exists()
        assert not (app_dir / GUARD_TEST).exists()


# ---------------------------------------------------------------------------
# KTD10: Docker image and certifi floor
# ---------------------------------------------------------------------------

# (variant, include_frontend, include_mobile)
VARIANTS = [
    pytest.param("full", True, True, id="full"),
    pytest.param("full", False, False, id="full-no-frontend"),
    pytest.param("lightweight-api", False, False, id="lightweight-api"),
    pytest.param("static-dashboard", True, False, id="static-dashboard"),
]


def _render_variant(tmp_path, variant, include_frontend, include_mobile, target) -> Path:
    """Render through the orchestrator's in-process wrapper.

    The Copier CLI fixture cannot render static-dashboard: two gated backend
    files both collapse to `app/.py` and the CLI stops at the conflict prompt.
    The wrapper renders with `overwrite=True`, as create-app does.
    """
    out = tmp_path / "out"
    out.mkdir()
    template_mod.render_template(
        template_dir=TEMPLATE_DIR,
        output_dir=out,
        data={
            "app_name": SLUG,
            "include_frontend": include_frontend,
            "include_mobile": include_mobile,
            "github_user": "testuser",
            "logto_endpoint": "https://logto.example.com",
            "deploy_target": target,
        },
        variant=variant,
    )
    return out / SLUG


def _node_tag_ok(tag: str) -> bool:
    m = re.fullmatch(r"22\.(\d+)\.(\d+)-slim", tag)
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= (23, 4)


class TestDockerfileCaAdvisory:
    @pytest.mark.parametrize(("variant", "include_frontend", "include_mobile"), VARIANTS)
    @pytest.mark.parametrize("target", ["railway", "render"])
    def test_dockerfile_patched(
        self, tmp_path, variant, include_frontend, include_mobile, target
    ):
        app_dir = _render_variant(tmp_path, variant, include_frontend, include_mobile, target)
        dockerfile = (app_dir / "Dockerfile").read_text()
        stages = re.split(r"^FROM ", dockerfile, flags=re.M)[1:]
        node_stages = [s for s in stages if s.startswith("node:")]
        python_stages = [s for s in stages if s.startswith("python:")]
        assert len(python_stages) == 1

        if include_frontend:
            assert len(node_stages) == 1
            tag = node_stages[0].split()[0].removeprefix("node:")
            assert _node_tag_ok(tag), f"node tag {tag!r} is below 22.23.4-slim"
        else:
            assert not node_stages

        # Python stage refreshes the CA bundle and cleans the apt lists.
        py = python_stages[0]
        m = re.search(r"^RUN apt-get update(.*?)(?=^\S)", py, flags=re.M | re.S)
        assert m, "Python stage does not run apt-get update"
        run = m.group(0)
        assert re.search(r"apt-get install[^\n]*ca-certificates", run)
        assert "rm -rf /var/lib/apt/lists/*" in run

        # Every ARG is VITE_* and lives in the Node stage only.
        for stage in stages:
            args = re.findall(r"^ARG\s+(\w+)", stage, flags=re.M)
            if stage.startswith("node:"):
                assert all(a.startswith("VITE_") for a in args), args
            else:
                assert not args, f"ARG outside the Node stage: {args}"

    def test_pyproject_requires_certifi_floor(self, copier_copy):
        text = (copier_copy() / "pyproject.toml").read_text()
        assert '"certifi>=2026.10.19"' in text


# ---------------------------------------------------------------------------
# Generated-app guard test
# ---------------------------------------------------------------------------


def _run_guard(app_dir: Path, tmp_path: Path, mutate=None) -> subprocess.CompletedProcess:
    """Run the generated guard test against a copy of the app's render.yaml.

    Only the guard file and render.yaml are copied, so the generated app's
    conftest (which imports the app) is not loaded.
    """
    work = tmp_path / "guard"
    (work / GUARD_TEST.parent).mkdir(parents=True)
    shutil.copy(app_dir / GUARD_TEST, work / GUARD_TEST)
    bp = _blueprint(app_dir)
    if mutate:
        mutate(bp)
    (work / "render.yaml").write_text(yaml.safe_dump(bp, sort_keys=False))
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(work / GUARD_TEST), "-q",
         "-p", "no:cacheprovider", "--rootdir", str(work)],
        capture_output=True, text=True, timeout=60, cwd=str(work),
    )


class TestGeneratedGuard:
    def test_guard_passes_on_rendered_app(self, render_full, tmp_path):
        result = _run_guard(render_full, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_guard_fails_without_ip_allow_list(self, render_full, tmp_path):
        def drop(bp):
            del bp["databases"][0]["ipAllowList"]

        result = _run_guard(render_full, tmp_path, drop)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "ipAllowList" in result.stdout

    def test_guard_fails_on_literal_secret_value(self, render_full, tmp_path):
        def add_secret(bp):
            bp["services"][0]["envVars"].append({"key": "STRIPE_API_KEY", "value": "sk_x"})

        result = _run_guard(render_full, tmp_path, add_secret)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "STRIPE_API_KEY" in result.stdout

    def test_guard_fails_on_literal_database_url(self, render_full, tmp_path):
        def add_url(bp):
            bp["services"][0]["envVars"].append(
                {"key": "DATABASE_URL", "value": "postgresql://user:password@host/db"}
            )

        result = _run_guard(render_full, tmp_path, add_url)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "DATABASE_URL" in result.stdout

    def test_guard_fails_on_literal_url_value_with_neutral_key(self, render_full, tmp_path):
        def add_conn(bp):
            bp["services"][0]["envVars"].append(
                {"key": "PRIMARY_DB", "value": "postgresql://user:password@host/db"}
            )

        result = _run_guard(render_full, tmp_path, add_conn)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "PRIMARY_DB" in result.stdout

    def test_guard_allows_non_secret_literal(self, render_full, tmp_path):
        def add_log_level(bp):
            bp["services"][0]["envVars"].append({"key": "LOG_LEVEL", "value": "info"})

        result = _run_guard(render_full, tmp_path, add_log_level)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_guard_absent_for_railway_target(self, copier_copy):
        assert not (copier_copy(deploy_target="railway") / GUARD_TEST).exists()
