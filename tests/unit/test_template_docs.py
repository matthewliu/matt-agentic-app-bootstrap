"""Track B Task #14: rendered AGENT_CONTEXT.md + README "Per-App Token Isolation" section.

Per spec §B5 the generated app ships two documentation surfaces that an agent
or human can read to onboard quickly:

1. ``AGENT_CONTEXT.md`` — a one-page primer that points at the authoritative
   ``<app> agent-context --json`` introspection. It documents the identity
   model (Logto M2M + ``X-<App>-Actor-Sub`` header), required env vars, and
   the 0-5 exit-code contract from §B2.

2. ``README.md`` gains a "Per-App Token Isolation" section that lists all
   eight invariants from spec §Track C / docs/SECURITY.md. The literal
   "Invariant 1" through "Invariant 8" labels must appear so reviewers and
   grep-based audits can map README content back to the canonical spec.

Per R10's variant × invariant matrix, ``AGENT_CONTEXT.md`` only renders when
there is a CLI to introspect — i.e. for every variant EXCEPT
``static-dashboard --no-auth`` (the include_cli=False branch). README's new
section, by contrast, always renders: it documents the security posture and
explicitly notes that Invariants 1-7 are N/A for the no-CLI variant while
Invariant 8 still applies.

Tests use the in-process Copier wrapper (``render_template``) for sub-second
render times.
"""

from __future__ import annotations

import re
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


# ---------------------------------------------------------------------------
# AGENT_CONTEXT.md presence / absence per R10 variant matrix.
# ---------------------------------------------------------------------------


class TestAgentContextPresence:
    """``AGENT_CONTEXT.md`` ships whenever a per-app CLI does (R10)."""

    def test_full_variant_has_agent_context(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        assert (rendered / "AGENT_CONTEXT.md").exists(), (
            "full variant must render AGENT_CONTEXT.md"
        )

    def test_lightweight_api_variant_has_agent_context(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="lightweight-api")
        assert (rendered / "AGENT_CONTEXT.md").exists(), (
            "lightweight-api variant must render AGENT_CONTEXT.md"
        )

    def test_static_dashboard_with_auth_has_agent_context(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=True)
        assert (rendered / "AGENT_CONTEXT.md").exists(), (
            "static-dashboard --with-auth must render AGENT_CONTEXT.md"
        )

    def test_static_dashboard_no_auth_omits_agent_context(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=False)
        agent_context = rendered / "AGENT_CONTEXT.md"
        assert not agent_context.exists(), (
            "static-dashboard --no-auth has no CLI so AGENT_CONTEXT.md must NOT "
            f"be rendered (found {agent_context})"
        )


# ---------------------------------------------------------------------------
# AGENT_CONTEXT.md content — must point at the slug-substituted CLI introspection.
# ---------------------------------------------------------------------------


class TestAgentContextContent:
    """AGENT_CONTEXT.md is an agent-facing primer pointing at the JSON contract."""

    def test_mentions_agent_context_json_with_slug(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full", app_name="foo")
        content = (rendered / "AGENT_CONTEXT.md").read_text()
        # Spec §B5: AGENT_CONTEXT.md "points at <app> agent-context --json
        # as the authoritative reference". Slug substitution must be live.
        assert "foo agent-context --json" in content, (
            "AGENT_CONTEXT.md must reference '<slug> agent-context --json' "
            f"with the rendered slug; got content snippet: {content[:200]!r}"
        )

    def test_slug_substitution_varies(self, tmp_path: Path):
        # Render with a different slug; the literal must update accordingly.
        rendered = _render(tmp_path, variant="full", app_name="bar-app")
        content = (rendered / "AGENT_CONTEXT.md").read_text()
        assert "bar-app agent-context --json" in content, (
            "AGENT_CONTEXT.md slug substitution failed for 'bar-app'"
        )
        # And the wrong slug must NOT appear (would indicate a hardcoded value).
        assert "foo agent-context --json" not in content

    def test_mentions_identity_model(self, tmp_path: Path):
        # Per §B2 + §B5, the primer must describe the M2M-token + actor-sub
        # identity model so agents know what auth shape to expect.
        rendered = _render(tmp_path, variant="full", app_name="demo-app")
        content = (rendered / "AGENT_CONTEXT.md").read_text()
        assert "M2M" in content, "AGENT_CONTEXT.md must mention M2M tokens"
        # Slug-substituted actor-sub header name (Title-cased, hyphen-joined).
        # For app_name='demo-app' the header is X-Demo-App-Actor-Sub.
        assert "Actor-Sub" in content, (
            "AGENT_CONTEXT.md must mention the X-<App>-Actor-Sub header model"
        )

    def test_mentions_token_cache_location(self, tmp_path: Path):
        # §B2: token cache lives at ~/.cache/<app_name>/token.json.
        rendered = _render(tmp_path, variant="full", app_name="demo-app")
        content = (rendered / "AGENT_CONTEXT.md").read_text()
        assert ".cache/demo-app" in content, (
            "AGENT_CONTEXT.md must point at the slug-scoped token cache path"
        )

    def test_mentions_required_env_vars(self, tmp_path: Path):
        # §B3 enumerates the consumer's required env vars; the primer must
        # at least name the slug-prefixed variants so agents can grep.
        rendered = _render(tmp_path, variant="full", app_name="demo-app")
        content = (rendered / "AGENT_CONTEXT.md").read_text()
        # Slug → snake_case → UPPER for env-var prefix (matches §B3).
        assert "DEMO_APP_M2M_APP_ID" in content, (
            "AGENT_CONTEXT.md must list <SLUG>_M2M_APP_ID env var"
        )
        assert "DEMO_APP_M2M_APP_SECRET" in content, (
            "AGENT_CONTEXT.md must list <SLUG>_M2M_APP_SECRET env var"
        )
        assert "DEMO_APP_USER_SUB" in content, (
            "AGENT_CONTEXT.md must list <SLUG>_USER_SUB env var"
        )

    @pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5])
    def test_mentions_each_exit_code(self, tmp_path: Path, code: int):
        # §B2 exit-code contract: 0..5 each MUST appear so an agent reading
        # the primer knows the contract without `--json` introspection.
        rendered = _render(tmp_path, variant="full")
        content = (rendered / "AGENT_CONTEXT.md").read_text()
        # Match "0 " or "0:" or "0 —" patterns rather than bare digit (would
        # collide with version strings etc.). Accepting any of those.
        assert (
            f"`{code}`" in content
            or f"{code} —" in content
            or f"{code} -" in content
            or f"{code}:" in content
            or f"| {code} |" in content
        ), (
            f"AGENT_CONTEXT.md must document exit code {code} (per spec §B2)"
        )


# ---------------------------------------------------------------------------
# README.md "Per-App Token Isolation" section.
# ---------------------------------------------------------------------------


class TestReadmePerAppIsolationSection:
    """README ships a Per-App Token Isolation section listing all 8 invariants."""

    def test_section_heading_present_full_variant(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        content = (rendered / "README.md").read_text()
        assert "Per-App Token Isolation" in content, (
            "README must contain a 'Per-App Token Isolation' section heading"
        )

    def test_section_heading_present_lightweight_api(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="lightweight-api")
        content = (rendered / "README.md").read_text()
        assert "Per-App Token Isolation" in content

    def test_section_heading_present_static_dashboard_with_auth(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=True)
        content = (rendered / "README.md").read_text()
        assert "Per-App Token Isolation" in content

    def test_section_heading_present_static_dashboard_no_auth(self, tmp_path: Path):
        # README always carries the section, even when the variant has no
        # CLI/auth — it documents which invariants do/don't apply.
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=False)
        content = (rendered / "README.md").read_text()
        assert "Per-App Token Isolation" in content

    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8])
    def test_invariant_n_present_full_variant(self, tmp_path: Path, n: int):
        rendered = _render(tmp_path, variant="full")
        content = (rendered / "README.md").read_text()
        assert f"Invariant {n}" in content, (
            f"README must mention 'Invariant {n}' literally so audits can grep "
            "for the canonical spec mapping"
        )

    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8])
    def test_invariant_n_present_lightweight_api(self, tmp_path: Path, n: int):
        rendered = _render(tmp_path, variant="lightweight-api")
        content = (rendered / "README.md").read_text()
        assert f"Invariant {n}" in content

    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8])
    def test_invariant_n_present_static_dashboard_with_auth(
        self, tmp_path: Path, n: int
    ):
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=True)
        content = (rendered / "README.md").read_text()
        assert f"Invariant {n}" in content

    @pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 6, 7, 8])
    def test_invariant_n_present_static_dashboard_no_auth(
        self, tmp_path: Path, n: int
    ):
        # Even the --no-auth variant carries all 8 invariant labels so the
        # README is grep-stable across variants. Applicability notes
        # ("N/A for this variant") may appear inline.
        rendered = _render(tmp_path, variant="static-dashboard", with_auth=False)
        content = (rendered / "README.md").read_text()
        assert f"Invariant {n}" in content


class TestReadmeStillHasOriginalContent:
    """The section is additive — existing README content stays intact."""

    def test_quick_start_still_present(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        content = (rendered / "README.md").read_text()
        # The original "Quick Start" section must still be there.
        assert "Quick Start" in content
        assert "uvicorn app.main:app" in content

    def test_deployment_section_still_present(self, tmp_path: Path):
        rendered = _render(tmp_path, variant="full")
        content = (rendered / "README.md").read_text()
        assert "Deployment" in content
        assert "Railway" in content


# ---------------------------------------------------------------------------
# Docs follow the deploy target (render-deploy-target U3, R2 + R7).
# ---------------------------------------------------------------------------

DOC_FILES = [
    "README.md",
    "CLAUDE.md",
    "AGENTS.md",
    ".env.example",
    "AGENT_CONTEXT.md",
    ".claude/memory/MEMORY.md",
]


def _render_target(tmp_path: Path, target: str) -> Path:
    # The default Render shape: full variant, frontend on, mobile off.
    return _render(
        tmp_path, variant="full", include_frontend=True, deploy_target=target
    )


class TestRenderTargetDocs:
    """A Render app's docs describe Render and never Railway."""

    @pytest.mark.parametrize("doc", DOC_FILES)
    def test_no_railway_mention(self, tmp_path: Path, doc: str):
        content = (_render_target(tmp_path, "render") / doc).read_text()
        assert "railway" not in content.lower(), (
            f"{doc} of a Render app must not mention Railway"
        )

    def test_readme_describes_blueprint(self, tmp_path: Path):
        content = (_render_target(tmp_path, "render") / "README.md").read_text()
        assert "render.yaml" in content
        assert "railway.json" not in content
        assert "https://render.com/deploy?repo=" in content
        assert "New > Blueprint" in content
        assert "manual deploy" in content

    def test_readme_describes_runtime_facts(self, tmp_path: Path):
        content = (_render_target(tmp_path, "render") / "README.md").read_text()
        assert "alembic -c app/db/migrations/alembic.ini upgrade head" in content
        assert "ipAllowList: []" in content
        assert "render ssh" in content
        assert "auth_not_configured" in content

    def test_claude_md_describes_blueprint(self, tmp_path: Path):
        content = (_render_target(tmp_path, "render") / "CLAUDE.md").read_text()
        assert "## Deployment (Render)" in content
        assert "render.yaml" in content
        assert "auth_not_configured" in content
        assert "render ssh" in content

    def test_backend_only_claude_md_describes_blueprint(self, tmp_path: Path):
        # The Railway deploy notes render only with a frontend. The Render
        # notes cover the Blueprint, so they render for every shape.
        rendered = _render(tmp_path, variant="full", deploy_target="render")
        content = (rendered / "CLAUDE.md").read_text()
        assert "## Deployment (Render)" in content

    def test_agent_context_keeps_cli_secrets_off_web_service(self, tmp_path: Path):
        content = (_render_target(tmp_path, "render") / "AGENT_CONTEXT.md").read_text()
        assert "Render passes every service env var to the Docker build" in content

    def test_env_example_has_no_scaffold_logto_ids(self, tmp_path: Path):
        # No Logto app exists for a Render app, so create-app fills nothing.
        # An empty LOGTO_APP_ID keeps local dev in the auth_not_configured state.
        content = (_render_target(tmp_path, "render") / ".env.example").read_text()
        assert re.search(r"^LOGTO_APP_ID=$", content, re.M)
        assert "<generated-by-scaffold>" not in content


class TestRailwayTargetDocs:
    """A Railway app keeps its Railway docs (R7)."""

    def test_readme_mentions_railway(self, tmp_path: Path):
        content = (_render_target(tmp_path, "railway") / "README.md").read_text()
        assert "Railway" in content
        assert "railway.json" in content
        assert "render.yaml" not in content

    def test_claude_md_keeps_railway_section(self, tmp_path: Path):
        content = (_render_target(tmp_path, "railway") / "CLAUDE.md").read_text()
        assert "## Deployment (Railway)" in content
        assert "### Railway CVE scanner" in content
        assert "Deployment (Render)" not in content


@pytest.mark.parametrize("target", ["render", "railway"])
class TestPlaceholderMarkers:
    """``.env.example`` markers are the tokens the docs describe."""

    def test_env_example_uses_host_neutral_marker(self, tmp_path: Path, target: str):
        content = (_render_target(tmp_path, target) / ".env.example").read_text()
        assert "LOGTO_APP_SECRET=<set-in-host>" in content
        assert "<set-in-railway>" not in content

    def test_readme_names_every_env_example_marker(self, tmp_path: Path, target: str):
        rendered = _render_target(tmp_path, target)
        env = (rendered / ".env.example").read_text()
        readme = (rendered / "README.md").read_text()
        markers = set(re.findall(r"^[A-Z_]+=(<[a-z-]+>)$", env, re.M))
        assert markers, ".env.example must use at least one placeholder marker"
        for marker in markers:
            assert f"`{marker}`" in readme, f"README must describe {marker}"
        assert "__SET_IN_RAILWAY__" not in readme

    def test_docs_do_not_claim_host_injects_database_url(
        self, tmp_path: Path, target: str
    ):
        content = (_render_target(tmp_path, target) / "CLAUDE.md").read_text()
        assert "auto-injects" not in content
