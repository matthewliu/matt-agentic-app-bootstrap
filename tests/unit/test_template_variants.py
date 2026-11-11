"""Tests for the static-dashboard + lightweight-api Copier variants (Task #15).

Spec §A3 + R10 matrix for what each variant SHIPS:

| variant                              | app/ (FastAPI) | frontend/ | mobile/ | app/cli/ |
|--------------------------------------|----------------|-----------|---------|----------|
| full                                 | yes            | yes       | yes     | yes      |
| lightweight-api                      | yes            | NO        | NO      | yes      |
| static-dashboard, with_auth=true     | NO             | yes       | NO      | yes (R10)|
| static-dashboard, with_auth=false    | NO             | yes       | NO      | NO       |

The conditional gates live in copier.yml's computed variables (`include_backend`,
`include_frontend`, `include_mobile`, `include_cli`) which the template uses in
directory-name Jinja (`{% if include_backend %}app{% endif %}/`) per the
pattern established by Task #11 for `app/cli/`.

These tests use the in-process Copier wrapper (`render_template`) so a full
render is sub-second — same fixture style as test_template_cli_rendering.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_core.bootstrap import template as template_mod


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"


def _render(
    tmp_path: Path,
    *,
    variant: str,
    with_auth: bool = True,
    app_name: str = "demo-app",
    include_frontend: bool | None = None,
    include_mobile: bool | None = None,
) -> Path:
    """Render the template and return the path to the rendered app dir.

    ``include_frontend`` / ``include_mobile`` default to the orchestrator's
    derivation (``variant == 'full'``) when not overridden, mirroring how the
    real `create-app` command computes them from `variant`.
    """
    if include_frontend is None:
        include_frontend = variant == "full" or variant == "static-dashboard"
    if include_mobile is None:
        include_mobile = variant == "full"

    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    template_mod.render_template(
        template_dir=TEMPLATE_DIR,
        output_dir=out,
        data={
            "app_name": app_name,
            "app_description": "smoke",
            "include_frontend": include_frontend,
            "include_mobile": include_mobile,
            "github_user": "testuser",
            "logto_endpoint": "https://logto.example.com",
            "with_auth": with_auth,
        },
        variant=variant,
    )
    return out / app_name


# ---------------------------------------------------------------------------
# Acceptance criteria for task #15 (one test per AC).
# ---------------------------------------------------------------------------


class TestFullVariant:
    """AC: full variant renders app/, frontend/, mobile/ (current shape)."""

    def test_full_renders_app_main(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        assert (rendered / "app" / "main.py").exists(), (
            "full variant must render app/main.py"
        )

    def test_full_renders_app_db(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        assert (rendered / "app" / "db" / "engine.py").exists()
        assert (rendered / "app" / "db" / "models.py").exists()

    def test_full_renders_frontend(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        assert (rendered / "frontend" / "package.json").exists(), (
            "full variant must render frontend/"
        )

    def test_full_renders_mobile(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        assert (rendered / "mobile" / "package.json").exists(), (
            "full variant must render mobile/"
        )


class TestLightweightApiVariant:
    """AC: lightweight-api renders app/ + cli/, but NO frontend/ or mobile/."""

    def test_renders_app_main(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="lightweight-api")
        assert (rendered / "app" / "main.py").exists(), (
            "lightweight-api variant must render app/main.py (FastAPI)"
        )

    def test_renders_app_db(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="lightweight-api")
        assert (rendered / "app" / "db" / "engine.py").exists(), (
            "lightweight-api variant must render app/db/engine.py (Postgres)"
        )

    def test_renders_app_cli(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="lightweight-api")
        assert (rendered / "app" / "cli" / "main.py").exists(), (
            "lightweight-api variant must render app/cli/ (R10 — auth shipped)"
        )

    def test_no_frontend_dir(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="lightweight-api")
        assert not (rendered / "frontend").exists(), (
            "lightweight-api must NOT render frontend/"
        )

    def test_no_mobile_dir(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="lightweight-api")
        assert not (rendered / "mobile").exists(), (
            "lightweight-api must NOT render mobile/"
        )

    def test_no_root_package_json(self, tmp_path: Path):
        # Root package.json is a workspace marker for the pnpm monorepo —
        # only meaningful when a frontend exists.
        rendered = _render(tmp_path, variant="lightweight-api")
        assert not (rendered / "package.json").exists(), (
            "lightweight-api must NOT render root package.json (no JS workspace)"
        )


class TestStaticDashboardVariant:
    """AC: static-dashboard renders frontend/ but NO FastAPI backend."""

    def test_no_app_main(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard")
        assert not (rendered / "app" / "main.py").exists(), (
            "static-dashboard must NOT render app/main.py (no FastAPI)"
        )

    def test_no_app_db(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard")
        assert not (rendered / "app" / "db" / "engine.py").exists(), (
            "static-dashboard must NOT render app/db/ by default (no Postgres)"
        )

    def test_renders_frontend(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard")
        assert (rendered / "frontend" / "package.json").exists(), (
            "static-dashboard must render frontend/ (the static bundle)"
        )

    def test_no_mobile_dir(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard")
        assert not (rendered / "mobile").exists(), (
            "static-dashboard must NOT render mobile/"
        )

    def test_with_auth_renders_cli(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=True)
        assert (rendered / "app" / "cli" / "main.py").exists(), (
            "static-dashboard --with-auth must render app/cli/ (R10)"
        )

    def test_no_auth_skips_cli(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=False)
        cli_dir = rendered / "app" / "cli"
        assert not cli_dir.exists(), (
            f"static-dashboard --no-auth must NOT render app/cli/ (found {cli_dir})"
        )


class TestComputedVariables:
    """copier.yml computed variables (include_backend etc.) are wired."""

    def test_include_backend_derived_from_variant(self):
        """copier.yml declares include_backend with a Jinja default."""
        import yaml

        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)

        assert "include_backend" in config, (
            "copier.yml must declare include_backend computed variable"
        )
        # Per spec: include_backend = variant != 'static-dashboard'
        assert "static-dashboard" in str(config["include_backend"]["default"]), (
            "include_backend default must reference 'static-dashboard' in its expression"
        )
        # Computed — never prompted.
        assert config["include_backend"].get("when") is False, (
            "include_backend must have when:false (computed, never prompted)"
        )

    def test_include_frontend_consistent(self):
        """include_frontend default still works for non-default variants."""
        # The user-facing default stays true (full), but the computed
        # expression in the orchestrator passes the variant-derived value
        # explicitly. The copier.yml field itself stays as a user toggle.
        import yaml

        with open(TEMPLATE_DIR / "copier.yml") as f:
            config = yaml.safe_load(f)
        assert config["include_frontend"]["default"] is True


class TestVariantRenderCleanliness:
    """No Jinja remnants or stray empty dirs across all variants."""

    @pytest.mark.parametrize("variant", ["full", "lightweight-api", "static-dashboard"])
    def test_no_jinja_remnants(self, tmp_path: Path, variant: str):
        rendered = _render(tmp_path, variant=variant)
        for f in rendered.rglob("*"):
            text_suffixes = (".py", ".ini", ".yml", ".yaml", ".json", ".md", ".toml")
            if f.is_file() and f.suffix in text_suffixes:
                content = f.read_text()
                # Variable left un-substituted
                assert "{{" not in content, (
                    f"variant={variant}: Jinja '{{{{' remnant in {f.relative_to(rendered)}"
                )
                # Block left un-rendered (allow Jinja inside templates that
                # were meant to be docs, but our generated files don't have
                # them)
                assert "{%" not in content, (
                    f"variant={variant}: Jinja '{{%' remnant in {f.relative_to(rendered)}"
                )

    @pytest.mark.parametrize("variant", ["full", "lightweight-api", "static-dashboard"])
    def test_no_empty_gate_dirs(self, tmp_path: Path, variant: str):
        """No literal '{% if ... %}' directory names left in the output."""
        rendered = _render(tmp_path, variant=variant)
        for p in rendered.rglob("*"):
            assert "{%" not in p.name, (
                f"variant={variant}: unrendered Jinja gate in path: {p.relative_to(rendered)}"
            )
            assert "{{" not in p.name, (
                f"variant={variant}: unrendered Jinja var in path: {p.relative_to(rendered)}"
            )
