"""Tests for conditional frontend template rendering."""

import json


class TestFrontendConditional:
    """Test that frontend directory is included/excluded based on include_frontend."""

    def test_frontend_dir_exists_when_enabled(self, copier_copy):
        """Frontend directory exists when include_frontend=true."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "frontend").exists()
        assert (result_dir / "frontend").is_dir()

    def test_frontend_dir_absent_when_disabled(self, copier_copy):
        """Frontend directory does not exist when include_frontend=false."""
        result_dir = copier_copy(include_frontend=False)
        assert not (result_dir / "frontend").exists()

    def test_main_py_has_static_mount_when_frontend(self, copier_copy):
        """main.py mounts static files when frontend is enabled."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "app" / "main.py").read_text()
        assert "StaticFiles" in content
        assert 'frontend/dist' in content


class TestFrontendPackageJson:
    """Test generated package.json."""

    def test_package_json_exists(self, copier_copy):
        """Frontend includes package.json."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "frontend" / "package.json").exists()

    def test_package_json_valid_json(self, copier_copy):
        """package.json is valid JSON."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "frontend" / "package.json") as f:
            pkg = json.load(f)
        assert "name" in pkg
        assert "dependencies" in pkg

    def test_package_json_has_react(self, copier_copy):
        """package.json includes React dependency."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "frontend" / "package.json") as f:
            pkg = json.load(f)
        assert "react" in pkg["dependencies"]
        assert "react-dom" in pkg["dependencies"]

    def test_package_json_has_logto(self, copier_copy):
        """package.json includes @logto/react."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "frontend" / "package.json") as f:
            pkg = json.load(f)
        assert "@logto/react" in pkg["dependencies"]

    def test_package_json_has_test_scripts(self, copier_copy):
        """package.json has test, test:watch, and test:coverage scripts."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "frontend" / "package.json") as f:
            pkg = json.load(f)
        assert "test" in pkg["scripts"]
        assert "test:watch" in pkg["scripts"]
        assert "test:coverage" in pkg["scripts"]
        assert "vitest run" in pkg["scripts"]["test"]

    def test_package_json_has_testing_dev_deps(self, copier_copy):
        """package.json has vitest and testing-library as dev deps."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "frontend" / "package.json") as f:
            pkg = json.load(f)
        dev = pkg["devDependencies"]
        assert "vitest" in dev
        assert "@testing-library/react" in dev
        assert "@testing-library/jest-dom" in dev
        assert "jsdom" in dev

    def test_package_json_app_name_substituted(self, copier_copy):
        """App name is substituted in package.json."""
        result_dir = copier_copy(app_name="my-cool-app", include_frontend=True)
        with open(result_dir / "frontend" / "package.json") as f:
            pkg = json.load(f)
        assert "my-cool-app" in pkg["name"]


class TestFrontendTestInfrastructure:
    """Test that frontend test infrastructure files exist."""

    def test_vitest_config_exists(self, copier_copy):
        """vitest.config.ts exists in frontend."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "frontend" / "vitest.config.ts").exists()

    def test_vitest_setup_exists(self, copier_copy):
        """vitest.setup.ts exists in frontend."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "frontend" / "vitest.setup.ts").exists()

    def test_vitest_config_uses_jsdom(self, copier_copy):
        """vitest.config.ts configures jsdom environment."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "vitest.config.ts").read_text()
        assert "jsdom" in content

    def test_vitest_setup_imports_jest_dom(self, copier_copy):
        """vitest.setup.ts imports @testing-library/jest-dom."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "vitest.setup.ts").read_text()
        assert "@testing-library/jest-dom" in content

    def test_test_files_exist(self, copier_copy):
        """Frontend __tests__/ directory has test files."""
        result_dir = copier_copy(include_frontend=True)
        tests_dir = result_dir / "frontend" / "src" / "__tests__"
        assert tests_dir.exists()
        test_files = list(tests_dir.glob("*.tsx"))
        assert len(test_files) >= 2, f"Expected at least 2 test files, found {len(test_files)}"


class TestFrontendSourceFiles:
    """Test generated frontend source files."""

    def test_app_tsx_exists(self, copier_copy):
        """App.tsx exists and references LogtoProvider."""
        result_dir = copier_copy(include_frontend=True)
        app_tsx = result_dir / "frontend" / "src" / "App.tsx"
        assert app_tsx.exists()
        content = app_tsx.read_text()
        assert "LogtoProvider" in content

    def test_layout_tsx_exists(self, copier_copy):
        """Layout.tsx exists with app name."""
        result_dir = copier_copy(app_name="my-app", include_frontend=True)
        layout = result_dir / "frontend" / "src" / "components" / "Layout.tsx"
        assert layout.exists()
        content = layout.read_text()
        assert "my-app" in content

    def test_main_tsx_exists(self, copier_copy):
        """main.tsx entry point exists."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "frontend" / "src" / "main.tsx").exists()

    def test_index_css_has_tailwind(self, copier_copy):
        """index.css includes Tailwind directives."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "src" / "index.css").read_text()
        assert "@tailwind base" in content
        assert "@tailwind components" in content
        assert "@tailwind utilities" in content

    def test_index_html_exists(self, copier_copy):
        """index.html exists at frontend root."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "frontend" / "index.html").exists()

    def test_tailwind_config_exists(self, copier_copy):
        """tailwind.config.ts exists."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "frontend" / "tailwind.config.ts").exists()

    def test_vite_config_has_proxy(self, copier_copy):
        """vite.config.ts proxies /api to FastAPI backend."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "vite.config.ts").read_text()
        assert "/api" in content
        assert "localhost:8000" in content

    def test_tsconfig_exists(self, copier_copy):
        """tsconfig.json exists."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "frontend" / "tsconfig.json").exists()

    def test_no_jinja_remnants_in_frontend(self, copier_copy):
        """No Jinja2 syntax remnants in any frontend file."""
        result_dir = copier_copy(include_frontend=True)
        frontend_dir = result_dir / "frontend"
        for f in frontend_dir.rglob("*"):
            if f.is_file() and f.suffix in (".tsx", ".ts", ".json", ".html", ".css", ".js"):
                content = f.read_text()
                assert "{{" not in content, f"Jinja remnant in {f.relative_to(result_dir)}"
                assert "{%" not in content, f"Jinja remnant in {f.relative_to(result_dir)}"


class TestAuthNotConfiguredFrontend:
    """KTD7: missing Logto config renders a panel instead of a blank page."""

    def test_config_ts_returns_result_instead_of_throwing(self, copier_copy):
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "src" / "config.ts").read_text()
        assert "throw" not in content
        assert "configured: false" in content
        assert "configured: true" in content

    def test_app_tsx_renders_panel_when_not_configured(self, copier_copy):
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "src" / "App.tsx").read_text()
        assert "Sign-in is not configured for this deployment" in content
        assert "<LogtoProvider" in content


class TestAuthNotConfiguredBackendTest:
    """The generated app ships a test for the no-auth lockout."""

    def test_generated_test_exists(self, copier_copy):
        result_dir = copier_copy(include_frontend=False)
        path = result_dir / "tests" / "integration" / "test_auth_not_configured.py"
        assert path.exists()
        content = path.read_text()
        assert "auth_not_configured" in content
        assert "{{" not in content
