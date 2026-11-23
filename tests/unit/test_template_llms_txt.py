"""The template's GET /llms.txt route: rendered for backend apps, off by default."""

import pytest

from tests.unit.test_template_variants import _render


@pytest.mark.parametrize("auth_provider", ["google", "logto"])
def test_backend_apps_get_the_route_and_its_test(copier_copy, auth_provider):
    app_dir = copier_copy(auth_provider=auth_provider, deploy_target="render")

    route = app_dir / "app" / "routes" / "llms_txt.py"
    assert route.is_file()
    assert (app_dir / "tests" / "integration" / "test_llms_txt.py").is_file()

    main = (app_dir / "app" / "main.py").read_text()
    assert "app.include_router(llms_txt.router)" in main

    config = (app_dir / "app" / "config.py").read_text()
    assert 'llms_txt_public_url: str = ""' in config
    assert "class LlmsTxtSettings" in config

    env = (app_dir / ".env.example").read_text()
    assert "\nLLMS_TXT_PUBLIC_URL=\n" in env


def test_frontend_catch_all_comes_after_the_route(copier_copy):
    app_dir = copier_copy(auth_provider="google", include_frontend=True)
    main = (app_dir / "app" / "main.py").read_text()
    route = main.index("app.include_router(llms_txt.router)")
    assert route < main.index('@app.get("/{path:path}")')


def test_llms_txt_settings_never_load_the_shared_env_file(copier_copy):
    config = (copier_copy(auth_provider="google") / "app" / "config.py").read_text()
    block = config[config.index("class LlmsTxtSettings") : config.index("llms_txt_settings =")]
    assert '"env_file": ".env"' in block


def test_render_blueprint_does_not_set_it(copier_copy):
    # Private apps must not be asked for the value when the Blueprint is applied.
    app_dir = copier_copy(auth_provider="google", deploy_target="render")
    assert "LLMS_TXT" not in (app_dir / "render.yaml").read_text()


def test_static_dashboard_gets_no_llms_txt(tmp_path):
    # No backend, so no route. The README must not describe one. The Copier
    # CLI cannot render this variant today (app/main.py and app/config.py
    # both become app/.py), so use the in-process helper the variant tests use.
    app_dir = _render(tmp_path, variant="static-dashboard", with_auth=False)
    assert not (app_dir / "app" / "routes").exists()
    assert not (app_dir / "tests" / "integration" / "test_llms_txt.py").exists()
    assert "LLMS_TXT" not in (app_dir / ".env.example").read_text()
    readme = (app_dir / "README.md").read_text()
    assert "llms" not in readme.lower()
    assert "\n\n\n" not in readme
    assert "llms" not in (app_dir / "AGENTS.md").read_text().lower()


@pytest.mark.parametrize("auth_provider", ["google", "logto"])
def test_backend_docs_describe_it(copier_copy, auth_provider):
    app_dir = copier_copy(auth_provider=auth_provider)
    readme = (app_dir / "README.md").read_text()
    assert "\n\n## llms.txt (public apps only)\n\n" in readme
    assert "\n\n\n" not in readme
    agents = (app_dir / "AGENTS.md").read_text()
    assert agents.endswith("See the llms.txt section of `README.md`.\n")
