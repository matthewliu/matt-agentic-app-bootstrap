"""Local Postgres helper: `compose.yaml` and the `.env.example` default.

A new user runs `docker compose up -d db`, copies `.env.example` to `.env`, and
starts the app. These checks keep the two files in step with each other and
with the Render database version.
"""

import re

import pytest
import yaml

DEFAULT_URL = "postgresql://postgres:postgres@localhost:5432/{name}"


def _env_value(app_dir, key):
    for line in (app_dir / ".env.example").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} missing from .env.example")


@pytest.mark.parametrize("deploy_target", ["render", "railway"])
def test_db_app_gets_local_postgres_that_matches_env_example(copier_copy, deploy_target):
    app = copier_copy(app_name="pg-app", deploy_target=deploy_target)
    compose = yaml.safe_load((app / "compose.yaml").read_text())
    db = compose["services"]["db"]

    assert db["image"].startswith("postgres:16")
    # Bound to the loopback address only: other computers cannot connect.
    assert db["ports"] == ["127.0.0.1:5432:5432"]
    env = db["environment"]
    assert (env["POSTGRES_USER"], env["POSTGRES_PASSWORD"], env["POSTGRES_DB"]) == (
        "postgres",
        "postgres",
        "pg-app",
    )
    assert _env_value(app, "DATABASE_URL") == DEFAULT_URL.format(name="pg-app")


def test_local_postgres_version_matches_render(copier_copy):
    app = copier_copy(app_name="pg-app", deploy_target="render")
    blueprint = yaml.safe_load((app / "render.yaml").read_text())
    render_major = blueprint["databases"][0]["postgresMajorVersion"]
    image = yaml.safe_load((app / "compose.yaml").read_text())["services"]["db"]["image"]
    assert re.match(rf"postgres:{render_major}\b", image), (image, render_major)


def test_app_without_db_gets_no_compose_file(copier_copy):
    app = copier_copy(app_name="no-db-app", with_db=False)
    assert not (app / "compose.yaml").exists()
    assert _env_value(app, "DATABASE_URL") == ""
