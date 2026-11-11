"""Tests for the [project.scripts] CLI entry-point added to pyproject.toml (Task #11).

Per spec §B2, every generated app exposes its CLI as an installable script::

    [project.scripts]
    <app_name> = "app.cli.main:app"

This is what makes `<slug> agent-context --json` invokable after
`pip install -e .` — agents need this exact entry-point shape so the
generated wrapper binary lands on PATH with the expected name.

Per R10, the scripts entry is present whenever `app/cli/` is rendered — i.e.
absent only for static-dashboard --no-auth.
"""

from __future__ import annotations

from pathlib import Path


from agentic_core.bootstrap import template as template_mod


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"


def _render(
    tmp_path: Path,
    *,
    variant: str = "full",
    with_auth: bool = True,
    app_name: str = "demo-app",
) -> Path:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    template_mod.render_template(
        template_dir=TEMPLATE_DIR,
        output_dir=out,
        data={
            "app_name": app_name,
            "app_description": "smoke",
            "include_frontend": False,
            "include_mobile": False,
            "github_user": "testuser",
            "logto_endpoint": "https://logto.example.com",
            "with_auth": with_auth,
        },
        variant=variant,
    )
    return out / app_name


def _read_pyproject(rendered: Path) -> str:
    return (rendered / "pyproject.toml").read_text()


class TestProjectScriptsPresent:
    """[project.scripts] block is rendered for variants that ship app/cli/."""

    def test_full_variant_has_scripts_entry(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full", app_name="foo")
        content = _read_pyproject(rendered)
        assert "[project.scripts]" in content, (
            "pyproject.toml must contain [project.scripts] in full variant"
        )
        # Exact entry: `foo = "app.cli.main:app"`
        assert 'foo = "app.cli.main:app"' in content, (
            "scripts entry must be exactly `foo = \"app.cli.main:app\"` "
            "(substituted from {{ app_name }})"
        )

    def test_lightweight_api_variant_has_scripts_entry(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="lightweight-api", app_name="bar")
        content = _read_pyproject(rendered)
        assert "[project.scripts]" in content
        assert 'bar = "app.cli.main:app"' in content

    def test_static_dashboard_with_auth_has_scripts_entry(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=True, app_name="baz")
        content = _read_pyproject(rendered)
        assert "[project.scripts]" in content
        assert 'baz = "app.cli.main:app"' in content


class TestScriptsEntryAbsentWhenNoCli:
    """static-dashboard --no-auth ships no app/cli/, so no scripts entry."""

    def test_static_dashboard_no_auth_omits_scripts_entry(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=False, app_name="qux")
        content = _read_pyproject(rendered)
        assert "[project.scripts]" not in content, (
            "static-dashboard --no-auth must NOT declare [project.scripts] — "
            "no CLI is generated"
        )
        assert 'qux = "app.cli.main:app"' not in content


class TestSlugSubstitution:
    """Slug substitution works for multi-word app_names (hyphens preserved)."""

    def test_multi_word_slug(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full", app_name="my-dashboard")
        content = _read_pyproject(rendered)
        # Hyphens are legal in [project.scripts] keys.
        assert 'my-dashboard = "app.cli.main:app"' in content


class TestPyprojectStillValid:
    """Rendered pyproject.toml must still parse as valid TOML."""

    def test_parses_as_toml(self, tmp_path: Path):
        import tomllib  # requires-python is >=3.12, so this always exists

        rendered = _render(tmp_path, variant="full", app_name="foo")
        with open(rendered / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)

        assert "project" in data
        assert "scripts" in data["project"], (
            "[project.scripts] section must round-trip through TOML parser"
        )
        assert data["project"]["scripts"]["foo"] == "app.cli.main:app"
