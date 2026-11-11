"""Tests for the per-app CLI subpackage scaffolded by Copier (Task #11, Track B).

Covers R10's variant × invariant applicability matrix for `app/cli/`:

| variant                              | app/cli/ rendered? |
|--------------------------------------|--------------------|
| full                                 | yes                |
| lightweight-api                      | yes                |
| static-dashboard, with_auth=true     | yes                |
| static-dashboard, with_auth=false    | NO                 |

Per R16, generated files mirror `agentic_core/output.py` and
`agentic_core/agent_context.py`; the mirror header comment must be present so
maintainers know to update both sides until v2 extracts a shared package.

Per PERF-005, the token-cache write path references `filelock` to prevent
mint storms when concurrent CLI invocations race for the same token.

These tests use the in-process Copier wrapper (`render_template`) added in
Task #6 so render is sub-second.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentic_core.bootstrap import template as template_mod


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"


def _render(
    tmp_path: Path,
    *,
    variant: str = "full",
    with_auth: bool = True,
    app_name: str = "demo-app",
    include_frontend: bool = False,
    include_mobile: bool = False,
    deploy_target: str = "railway",
) -> Path:
    """Render the template and return the path to the rendered app dir."""
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
            "deploy_target": deploy_target,
        },
        variant=variant,
    )
    return out / app_name


class TestVariantMatrix:
    """R10 matrix: `app/cli/` ships unless static-dashboard --no-auth."""

    def test_full_variant_renders_cli(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        assert (rendered / "app" / "cli" / "main.py").exists(), (
            "full variant must render app/cli/main.py"
        )

    def test_lightweight_api_variant_renders_cli(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="lightweight-api")
        assert (rendered / "app" / "cli" / "main.py").exists(), (
            "lightweight-api variant must render app/cli/main.py"
        )

    def test_static_dashboard_with_auth_renders_cli(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=True)
        assert (rendered / "app" / "cli" / "main.py").exists(), (
            "static-dashboard --with-auth must render app/cli/main.py"
        )

    def test_static_dashboard_no_auth_skips_cli(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=False)
        cli_dir = rendered / "app" / "cli"
        assert not cli_dir.exists(), (
            f"static-dashboard --no-auth must NOT render app/cli/ (found {cli_dir})"
        )


class TestCliFilesPresent:
    """All required files in the app/cli/ subpackage are rendered."""

    @pytest.fixture
    def cli_dir(self, tmp_path: Path) -> Path:
        rendered = _render(tmp_path, variant="full")
        return rendered / "app" / "cli"

    @pytest.mark.parametrize(
        "relpath",
        [
            "__init__.py",
            "main.py",
            "client.py",
            "output.py",
            "agent_context.py",
            "commands/__init__.py",
            "commands/example.py",
        ],
    )
    def test_file_exists(self, cli_dir: Path, relpath: str):
        path = cli_dir / relpath
        assert path.exists(), f"expected {path} to exist after render"


class TestMirrorHeaders:
    """R16: output.py and agent_context.py carry mirror-comment headers."""

    def test_output_has_mirror_header(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        first_line = (rendered / "app" / "cli" / "output.py").read_text().splitlines()[0]
        assert "Mirror of agentic_core/output.py" in first_line, (
            f"output.py first line must contain R16 mirror notice; got: {first_line!r}"
        )

    def test_agent_context_has_mirror_header(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        first_line = (
            rendered / "app" / "cli" / "agent_context.py"
        ).read_text().splitlines()[0]
        assert "Mirror of agentic_core/agent_context.py" in first_line, (
            f"agent_context.py first line must contain R16 mirror notice; got: {first_line!r}"
        )


class TestTokenCachePerf005:
    """PERF-005: token cache write uses filelock to prevent mint storms."""

    def test_client_references_filelock(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full", app_name="foo-app")
        content = (rendered / "app" / "cli" / "client.py").read_text()
        assert "filelock" in content, (
            "client.py must import/reference filelock for PERF-005 mint-storm guard"
        )
        assert "FileLock" in content, (
            "client.py must use FileLock(...) around the token cache check/write"
        )

    def test_client_token_cache_path_is_slug_scoped(self, tmp_path: Path):
        # Cache path is ~/.cache/<app_name>/token.json — must contain the
        # rendered app slug literal so multiple apps don't collide.
        rendered = _render(tmp_path, variant="full", app_name="foo-app")
        content = (rendered / "app" / "cli" / "client.py").read_text()
        assert ".cache/foo-app" in content or '"foo-app"' in content, (
            "client.py token cache path must be scoped to the app slug 'foo-app' "
            "(found neither '.cache/foo-app' nor '\"foo-app\"' literal)"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
    def test_client_documents_mode_0600(self, tmp_path: Path):
        # We don't actually run the CLI here (would require a venv install);
        # instead we assert the implementation declares the 0o600 mode literal
        # so reviewers can verify by reading the rendered file.
        rendered = _render(tmp_path, variant="full")
        content = (rendered / "app" / "cli" / "client.py").read_text()
        assert "0o600" in content, (
            "client.py must set mode 0o600 on the token cache (see PERF-005)"
        )


class TestCliCompiles:
    """The rendered Python is syntactically valid."""

    @pytest.mark.parametrize(
        "relpath",
        [
            "__init__.py",
            "main.py",
            "client.py",
            "output.py",
            "agent_context.py",
            "commands/__init__.py",
            "commands/example.py",
        ],
    )
    def test_compile(self, tmp_path: Path, relpath: str):
        import py_compile

        rendered = _render(tmp_path, variant="full")
        path = rendered / "app" / "cli" / relpath
        # py_compile raises PyCompileError on syntax errors — surfaces stack.
        py_compile.compile(str(path), doraise=True)


class TestAgentContextDeployTarget:
    """agent_context.py names the host the app deploys to (render-deploy-target U3)."""

    def test_render_target_names_render(self, tmp_path: Path):
        import py_compile

        rendered = _render(tmp_path, variant="full", deploy_target="render")
        path = rendered / "app" / "cli" / "agent_context.py"
        content = path.read_text()
        assert "Railway" not in content
        assert "Render service name" in content
        assert "Render env on the demo-app" in content
        py_compile.compile(str(path), doraise=True)

    def test_railway_target_names_railway(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full", deploy_target="railway")
        content = (rendered / "app" / "cli" / "agent_context.py").read_text()
        assert "Railway project name" in content
        assert "Railway env on the demo-app" in content


class TestExampleCommandShape:
    """commands/example.py demonstrates the M2M token → API → output flow."""

    def test_example_uses_get_client(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        content = (rendered / "app" / "cli" / "commands" / "example.py").read_text()
        # Must call get_client() (M2M token + API call abstraction) and
        # render output via the output module so the --json contract holds.
        assert "get_client" in content, (
            "example.py must call get_client() to mint M2M token + make API call"
        )

    def test_example_supports_json_flag(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        content = (rendered / "app" / "cli" / "commands" / "example.py").read_text()
        assert "--json" in content or "json_output" in content, (
            "example.py must expose --json flag for machine-readable output"
        )
