"""Tests for copier.yml configuration and basic template rendering."""

import subprocess

import yaml

from tests.conftest import TEMPLATE_DIR


class TestCopierYml:
    """Validate copier.yml syntax and completeness."""

    def test_copier_yml_exists(self):
        """copier.yml file exists at the expected location."""
        assert (TEMPLATE_DIR / "copier.yml").exists()

    def test_copier_yml_valid_yaml(self):
        """copier.yml parses as valid YAML."""
        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)
        assert isinstance(config, dict)

    def test_required_variables_defined(self):
        """All required template variables are defined."""
        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)

        required_vars = [
            "app_name",
            "app_description",
            "python_package_name",
            "include_frontend",
            "github_user",
            "logto_endpoint",
        ]
        for var in required_vars:
            assert var in config, f"Missing required variable: {var}"

    def test_variable_types(self):
        """Template variables have correct types specified."""
        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)

        assert config["app_name"]["type"] == "str"
        assert config["app_description"]["type"] == "str"
        assert config["include_frontend"]["type"] == "bool"
        assert config["github_user"]["type"] == "str"
        assert config["logto_endpoint"]["type"] == "str"

    def test_subdirectory_set(self):
        """_subdirectory is set to 'template'."""
        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)
        assert config.get("_subdirectory") == "template"

    def test_skip_if_exists_configured(self):
        """_skip_if_exists includes .env and memory file."""
        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)
        skip = config.get("_skip_if_exists", [])
        assert ".env" in skip
        assert ".claude/memory/MEMORY.md" in skip

    def test_app_name_has_validator(self):
        """app_name has a regex validator."""
        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)
        assert "validator" in config["app_name"]
        assert "regex_search" in config["app_name"]["validator"]

    def test_include_frontend_defaults_true(self):
        """include_frontend defaults to true."""
        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)
        assert config["include_frontend"]["default"] is True

    def test_deploy_target_question(self):
        """deploy_target offers render and railway and defaults to railway.

        The default stays railway so `copier update` on an app whose answers
        file predates the question keeps rendering railway.json (KTD4).
        """
        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)
        q = config["deploy_target"]
        assert q["type"] == "str"
        assert sorted(q["choices"]) == ["railway", "render"]
        assert q["default"] == "railway"


class TestCopierCopyBasic:
    """Test basic copier copy with default variables."""

    def test_copier_copy_succeeds(self, copier_copy):
        """copier copy with minimal args produces a directory."""
        result_dir = copier_copy(include_frontend=False)
        assert result_dir.exists()

    def test_core_app_files_exist(self, copier_copy):
        """Generated app has core Python files."""
        result_dir = copier_copy(include_frontend=False)
        app_dir = result_dir / "app"
        assert app_dir.exists()
        assert (app_dir / "__init__.py").exists()
        assert (app_dir / "main.py").exists()
        assert (app_dir / "config.py").exists()
        assert (app_dir / "routes" / "health.py").exists()

    def test_main_py_compiles(self, copier_copy):
        """Generated main.py is valid Python syntax."""
        result_dir = copier_copy(include_frontend=False)
        main_py = result_dir / "app" / "main.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(main_py)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"main.py compile error: {result.stderr}"

    def test_config_py_compiles(self, copier_copy):
        """Generated config.py is valid Python syntax."""
        result_dir = copier_copy(include_frontend=False)
        config_py = result_dir / "app" / "config.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(config_py)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"config.py compile error: {result.stderr}"

    def test_health_py_compiles(self, copier_copy):
        """Generated health.py is valid Python syntax."""
        result_dir = copier_copy(include_frontend=False)
        health_py = result_dir / "app" / "routes" / "health.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(health_py)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"health.py compile error: {result.stderr}"

    def test_no_jinja_remnants_in_output(self, copier_copy):
        """Generated files should not contain raw Jinja2 syntax."""
        result_dir = copier_copy(include_frontend=False)
        for py_file in result_dir.rglob("*.py"):
            content = py_file.read_text()
            assert "{{" not in content, f"Jinja remnant '{{{{' in {py_file}"
            assert "{%" not in content, f"Jinja remnant '{{% %}}' in {py_file}"

    def test_app_name_substituted(self, copier_copy):
        """app_name variable is substituted in generated files."""
        result_dir = copier_copy(app_name="my-test-app", include_frontend=False)
        config_py = result_dir / "app" / "config.py"
        content = config_py.read_text()
        assert "my-test-app" in content

    def test_no_frontend_dir_when_disabled(self, copier_copy):
        """No frontend directory when include_frontend is false."""
        result_dir = copier_copy(include_frontend=False)
        assert not (result_dir / "frontend").exists()

    def test_main_py_no_static_mount_when_no_frontend(self, copier_copy):
        """main.py should not mount static files when frontend is disabled."""
        result_dir = copier_copy(include_frontend=False)
        main_py = result_dir / "app" / "main.py"
        content = main_py.read_text()
        assert "StaticFiles" not in content
