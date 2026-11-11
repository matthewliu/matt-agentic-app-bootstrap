"""Tests for ``agentic_core.bootstrap.template``.

Covers PERF-004 (Copier invoked in-process — no subprocess shell-out), the
three template variants (full, lightweight-api, static-dashboard once Task
#15 adds them; ``full`` works today), and opt-out flag forwarding through
the ``data`` dict.

Note on variant coverage: today ``copier.yml`` only ships the ``full``
variant. ``static-dashboard`` and ``lightweight-api`` are added by Task #15.
The wrapper here MUST accept all three variant names (so #15 can plug in
without changing the wrapper) but we only end-to-end render ``full`` until
the other variants exist. The tests that need variant data dict forwarding
use mocks so we don't depend on #15.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agentic_core.bootstrap import template as template_mod


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"


class TestInProcessInvocation:
    """PERF-004: Copier runs via the Python API, never via subprocess."""

    def test_calls_copier_run_copy(self, tmp_path: Path):
        # AC: copier.run_copy() invoked in-process.
        with patch("agentic_core.bootstrap.template.run_copy") as mock_run:
            template_mod.render_template(
                template_dir=TEMPLATE_DIR,
                output_dir=tmp_path / "out",
                data={"app_name": "foo"},
                variant="full",
            )
            assert mock_run.called, "render_template must call copier.run_copy"

    def test_does_not_shell_out_to_copier(self, tmp_path: Path):
        # AC: subprocess.run is NOT called with 'copier' arg.
        with patch("agentic_core.bootstrap.template.run_copy"), patch(
            "subprocess.run"
        ) as mock_subprocess:
            template_mod.render_template(
                template_dir=TEMPLATE_DIR,
                output_dir=tmp_path / "out",
                data={"app_name": "foo"},
                variant="full",
            )
            # Walk every subprocess.run invocation and assert none used
            # 'copier' as the binary.
            for call in mock_subprocess.call_args_list:
                args, _kwargs = call.args, call.kwargs
                # First positional is usually the cmd list/str.
                if args:
                    cmd = args[0]
                    if isinstance(cmd, (list, tuple)):
                        assert "copier" not in cmd[0], (
                            f"render_template must not shell out to copier "
                            f"(found cmd={cmd!r})"
                        )
                    elif isinstance(cmd, str):
                        assert "copier" not in cmd.split()[0], (
                            f"render_template must not shell out to copier "
                            f"(found cmd={cmd!r})"
                        )


class TestVariantDispatch:
    """Variant name must be forwarded to Copier's ``data`` dict (so the
    template can render conditionally on it). We accept all three even when
    only ``full`` ships today.
    """

    @pytest.mark.parametrize("variant", ["full", "lightweight-api", "static-dashboard"])
    def test_variant_passed_through_to_data(self, tmp_path: Path, variant: str):
        with patch("agentic_core.bootstrap.template.run_copy") as mock_run:
            template_mod.render_template(
                template_dir=TEMPLATE_DIR,
                output_dir=tmp_path / "out",
                data={"app_name": "foo"},
                variant=variant,
            )
            # copier.run_copy receives data via the `data=` kwarg.
            kwargs = mock_run.call_args.kwargs
            assert "data" in kwargs, "render_template must pass data= to run_copy"
            data = kwargs["data"]
            assert data.get("variant") == variant, (
                f"variant {variant!r} must be forwarded into Copier data "
                f"(got {data.get('variant')!r})"
            )

    def test_default_variant_is_full(self, tmp_path: Path):
        with patch("agentic_core.bootstrap.template.run_copy") as mock_run:
            template_mod.render_template(
                template_dir=TEMPLATE_DIR,
                output_dir=tmp_path / "out",
                data={"app_name": "foo"},
            )
            kwargs = mock_run.call_args.kwargs
            assert kwargs["data"].get("variant") == "full"

    def test_rejects_unknown_variant(self, tmp_path: Path):
        with pytest.raises(ValueError, match="variant"):
            template_mod.render_template(
                template_dir=TEMPLATE_DIR,
                output_dir=tmp_path / "out",
                data={"app_name": "foo"},
                variant="franken-variant",
            )


class TestDataForwarding:
    """Opt-out flags (with_auth, with_db, with_frontend, with_mobile) must
    flow through to Copier so the template can conditionally include
    sections.
    """

    def test_forwards_supplied_data(self, tmp_path: Path):
        with patch("agentic_core.bootstrap.template.run_copy") as mock_run:
            template_mod.render_template(
                template_dir=TEMPLATE_DIR,
                output_dir=tmp_path / "out",
                data={
                    "app_name": "foo",
                    "with_auth": False,
                    "with_db": True,
                    "with_frontend": False,
                    "with_mobile": False,
                },
                variant="lightweight-api",
            )
            data = mock_run.call_args.kwargs["data"]
            assert data["app_name"] == "foo"
            assert data["with_auth"] is False
            assert data["with_db"] is True
            assert data["with_frontend"] is False
            assert data["with_mobile"] is False

    def test_paths_passed_as_strings(self, tmp_path: Path):
        # copier.run_copy expects src as str; output dir accepts Path|str.
        # We normalize to str inside render_template for consistency.
        out = tmp_path / "out"
        with patch("agentic_core.bootstrap.template.run_copy") as mock_run:
            template_mod.render_template(
                template_dir=TEMPLATE_DIR,
                output_dir=out,
                data={"app_name": "foo"},
                variant="full",
            )
            kwargs = mock_run.call_args.kwargs
            assert kwargs["src_path"] == str(TEMPLATE_DIR)
            # dst_path accepts Path or str — assert one of them.
            assert str(kwargs["dst_path"]) == str(out)

    def test_defaults_and_skip_tasks(self, tmp_path: Path):
        # We want non-interactive (defaults=True) and we don't want
        # Copier's _tasks (venv + pip install + git init) to fire during
        # tests/agent runs — the orchestrator handles those steps
        # separately and they wreck test isolation.
        with patch("agentic_core.bootstrap.template.run_copy") as mock_run:
            template_mod.render_template(
                template_dir=TEMPLATE_DIR,
                output_dir=tmp_path / "out",
                data={"app_name": "foo"},
                variant="full",
            )
            kwargs = mock_run.call_args.kwargs
            assert kwargs.get("defaults") is True
            # skip_tasks is critical: _tasks in copier.yml runs npm/pip/git
            # which we don't want during scaffold.
            assert kwargs.get("skip_tasks") is True


class TestEndToEndRender:
    """Smoke test against the real Copier and the real ``full`` variant
    template. Proves the wrapper actually works, not just that it forwards
    args.
    """

    def test_full_variant_renders_app(self, tmp_path: Path):
        out = tmp_path / "out"
        out.mkdir()
        template_mod.render_template(
            template_dir=TEMPLATE_DIR,
            output_dir=out,
            data={
                "app_name": "smoke-test-app",
                "app_description": "smoke",
                "include_frontend": False,
                "include_mobile": False,
                "github_user": "testuser",
                "logto_endpoint": "https://logto.example.com",
            },
            variant="full",
        )
        # Copier renders template/{{ app_name }}/ → out/smoke-test-app/.
        rendered = out / "smoke-test-app"
        assert rendered.exists(), f"expected rendered dir at {rendered}"
        # Spot-check a couple of expected files.
        assert (rendered / "app" / "main.py").exists()
        assert (rendered / "pyproject.toml").exists()
