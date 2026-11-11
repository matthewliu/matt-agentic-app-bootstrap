"""Tests that all generated Python files are syntactically valid."""

import subprocess


class TestAllPythonFilesCompile:
    """Verify every generated .py file passes py_compile."""

    def test_all_py_files_compile_no_frontend(self, copier_copy):
        """Every .py file in the generated app (no frontend) compiles."""
        result_dir = copier_copy(include_frontend=False)
        py_files = list(result_dir.rglob("*.py"))
        assert len(py_files) > 0, "No Python files found in generated app"

        failures = []
        for py_file in py_files:
            result = subprocess.run(
                ["python3", "-m", "py_compile", str(py_file)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                failures.append(f"{py_file.relative_to(result_dir)}: {result.stderr.strip()}")

        assert not failures, (
            f"{len(failures)} Python file(s) failed to compile:\n" + "\n".join(failures)
        )

    def test_all_py_files_compile_with_frontend(self, copier_copy):
        """Every .py file in the generated app (with frontend) compiles."""
        result_dir = copier_copy(include_frontend=True)
        py_files = list(result_dir.rglob("*.py"))
        assert len(py_files) > 0

        failures = []
        for py_file in py_files:
            result = subprocess.run(
                ["python3", "-m", "py_compile", str(py_file)],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                failures.append(f"{py_file.relative_to(result_dir)}: {result.stderr.strip()}")

        assert not failures, (
            f"{len(failures)} Python file(s) failed to compile:\n" + "\n".join(failures)
        )

    def test_expected_python_file_count(self, copier_copy):
        """Generated app has the expected number of Python files."""
        result_dir = copier_copy(include_frontend=False)
        py_files = list(result_dir.rglob("*.py"))
        # app/__init__.py, main.py, config.py, routes/__init__.py, health.py, items.py,
        # models/__init__.py, item.py, services/__init__.py, item_service.py,
        # db/__init__.py, engine.py, models.py, migrations/env.py,
        # migrations/versions/001_initial.py,
        # auth/__init__.py, middleware.py,
        # tests/__init__.py, conftest.py, unit/__init__.py, test_auth.py, test_item_service.py,
        # integration/__init__.py, test_items_api.py
        assert len(py_files) >= 20, f"Expected >= 20 Python files, found {len(py_files)}"


class TestNoJinjaRemnantsComprehensive:
    """Comprehensive check for Jinja2 remnants across all file types."""

    def test_no_jinja_remnants_no_frontend(self, copier_copy):
        """No Jinja2 syntax in any file when frontend is disabled."""
        result_dir = copier_copy(include_frontend=False)
        check_extensions = {".py", ".toml", ".json", ".yml", ".yaml", ".ini", ".md", ".txt"}
        failures = []
        for f in result_dir.rglob("*"):
            if f.is_file() and f.suffix in check_extensions:
                content = f.read_text()
                if "{{" in content or "{%" in content:
                    failures.append(str(f.relative_to(result_dir)))
        assert not failures, f"Jinja remnants found in: {', '.join(failures)}"

    def test_no_jinja_remnants_with_frontend(self, copier_copy):
        """No Jinja2 syntax in any file when frontend is enabled."""
        result_dir = copier_copy(include_frontend=True)
        check_extensions = {
            ".py", ".toml", ".json", ".yml", ".yaml", ".ini", ".md", ".txt",
            ".tsx", ".ts", ".html", ".css", ".js",
        }
        failures = []
        for f in result_dir.rglob("*"):
            if f.is_file() and f.suffix in check_extensions:
                content = f.read_text()
                if "{{" in content or "{%" in content:
                    failures.append(str(f.relative_to(result_dir)))
        assert not failures, f"Jinja remnants found in: {', '.join(failures)}"


class TestCustomAppNameSubstitution:
    """Verify app name substitution across all key files."""

    def test_app_name_in_all_key_files(self, copier_copy):
        """Custom app name appears in CLAUDE.md, pyproject.toml, README.md, config.py."""
        result_dir = copier_copy(app_name="my-special-app", include_frontend=False)

        files_to_check = [
            ("CLAUDE.md", "my-special-app"),
            ("README.md", "my-special-app"),
            ("pyproject.toml", "my-special-app"),
            ("app/config.py", "my-special-app"),
            (".env.example", "my-special-app"),
        ]
        for filepath, expected in files_to_check:
            content = (result_dir / filepath).read_text()
            assert expected in content, (
                f"Expected '{expected}' in {filepath}"
            )
