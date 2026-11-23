"""Tests for generated AGENTS.md, CLAUDE.md, PRODUCT.md, README.md, and .claude/ scaffolding."""

import json


import pytest

# Generated AGENTS.md is the guide for every coding agent. Codex and Muse read
# AGENTS.md first (Muse ignores CLAUDE.md when AGENTS.md exists), so the rules
# must live there. CLAUDE.md only imports it for Claude Code.
# The limit covers Render apps without mobile, the shapes create-app makes.
# Railway apps with a frontend and Logto apps with mobile are over it; those
# options are due for removal. Root AGENTS.md states the same scope.
LINE_LIMIT = 100
REPO_ROOT = __import__("pathlib").Path(__file__).parents[2]


def _line_count(text: str) -> int:
    return len(text.strip().split("\n"))


class TestAgentsMd:
    """Validate generated AGENTS.md, the canonical agent guide."""

    def test_agents_md_contains_app_name_and_description(self, copier_copy):
        result_dir = copier_copy(
            app_name="habit-tracker", app_description="Track daily habits", include_frontend=False
        )
        content = (result_dir / "AGENTS.md").read_text()
        assert content.startswith("# habit-tracker\n")
        assert "Track daily habits" in content

    def test_agents_md_has_commands(self, copier_copy):
        content = (copier_copy(include_frontend=False) / "AGENTS.md").read_text()
        assert "pytest" in content
        assert "ruff check" in content
        assert "uvicorn" in content
        assert "alembic" in content

    def test_agents_md_has_architecture(self, copier_copy):
        content = (copier_copy(include_frontend=False) / "AGENTS.md").read_text()
        for directory in ("app/routes/", "app/models/", "app/services/", "app/db/", "app/auth/"):
            assert directory in content

    def test_agents_md_points_to_product_md(self, copier_copy):
        content = (copier_copy(include_frontend=False) / "AGENTS.md").read_text()
        assert "`PRODUCT.md`" in content

    @pytest.mark.parametrize("auth_provider", ["google", "logto"])
    @pytest.mark.parametrize("include_frontend", [False, True])
    def test_agents_md_under_line_limit(self, copier_copy, auth_provider, include_frontend):
        result_dir = copier_copy(
            auth_provider=auth_provider,
            include_frontend=include_frontend,
            include_mobile=False,
            deploy_target="render",
        )
        count = _line_count((result_dir / "AGENTS.md").read_text())
        assert count < LINE_LIMIT, (
            f"AGENTS.md ({auth_provider}, frontend={include_frontend}) is {count} lines"
        )

    def test_agents_md_includes_frontend_when_enabled(self, copier_copy):
        content = (copier_copy(include_frontend=True) / "AGENTS.md").read_text()
        assert "| `frontend/`" in content
        assert "`frontend/src/design/tokens.ts`" in content
        assert "### Type sync contract" in content

    @pytest.mark.parametrize("auth_provider", ["google", "logto"])
    def test_agents_md_is_the_guide_not_a_pointer(self, copier_copy, auth_provider):
        content = (copier_copy(auth_provider=auth_provider) / "AGENTS.md").read_text()
        assert "Read `CLAUDE.md`" not in content
        assert "## Conventions" in content

    def test_no_generated_file_sends_readers_to_claude_md(self, copier_copy):
        result_dir = copier_copy(include_frontend=True, auth_provider="google")
        for path in result_dir.rglob("*"):
            if not path.is_file() or path.name in ("AGENTS.md", "CLAUDE.md"):
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            assert "see CLAUDE.md" not in text.lower().replace("`", ""), path

    def test_agents_md_no_frontend_when_disabled(self, copier_copy):
        content = (copier_copy(include_frontend=False) / "AGENTS.md").read_text()
        assert "| `frontend/`" not in content


class TestClaudeMd:
    """Generated CLAUDE.md only imports AGENTS.md."""

    @pytest.mark.parametrize("auth_provider", ["google", "logto"])
    def test_claude_md_imports_agents_md(self, copier_copy, auth_provider):
        result_dir = copier_copy(app_name="habit-tracker", auth_provider=auth_provider)
        content = (result_dir / "CLAUDE.md").read_text()
        assert content.startswith("# habit-tracker\n")
        assert "\n@AGENTS.md\n" in content
        # No rules of its own, so the two files cannot disagree.
        assert _line_count(content) <= 5
        assert "pytest" not in content

    def test_root_claude_md_imports_root_agents_md(self):
        content = (REPO_ROOT / "CLAUDE.md").read_text()
        assert "\n@AGENTS.md\n" in content
        assert _line_count(content) <= 5
        assert "## Security boundaries" in (REPO_ROOT / "AGENTS.md").read_text()


class TestProductMd:
    """Generated PRODUCT.md is a short template the owner fills in."""

    def test_product_md_exists_with_sections(self, copier_copy):
        result_dir = copier_copy(app_name="habit-tracker", app_description="Track daily habits")
        content = (result_dir / "PRODUCT.md").read_text()
        assert content.startswith("# habit-tracker: product\n")
        assert "Track daily habits" in content
        for section in ("## Who it is for", "## What it does not do", "## Data and privacy"):
            assert section in content
        assert "`LLMS_TXT_PUBLIC_URL`" in content
        assert "(CLI or MCP)" in content

    def test_readme_tells_owner_to_fill_in_product_md(self, copier_copy):
        content = (copier_copy() / "README.md").read_text()
        assert "Fill in `PRODUCT.md` first." in content

    def test_copier_update_never_overwrites_product_md(self):
        import yaml
        from pathlib import Path

        copier_yml = Path(__file__).parents[2] / "templates" / "mini-app" / "copier.yml"
        assert "PRODUCT.md" in yaml.safe_load(copier_yml.read_text())["_skip_if_exists"]


class TestReadmeMd:
    """Validate generated README.md."""

    def test_readme_exists(self, copier_copy):
        """Generated app includes README.md."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "README.md").exists()

    def test_readme_has_app_name(self, copier_copy):
        """README.md contains the app name."""
        result_dir = copier_copy(app_name="habit-tracker", include_frontend=False)
        content = (result_dir / "README.md").read_text()
        assert "habit-tracker" in content

    def test_readme_has_quick_start(self, copier_copy):
        """README.md has quick start instructions."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "README.md").read_text()
        assert "Quick Start" in content
        assert "pip install" in content
        assert "uvicorn" in content

    def test_readme_has_api_docs_link(self, copier_copy):
        """README.md links to FastAPI auto-generated docs."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "README.md").read_text()
        assert "/docs" in content

    def test_readme_has_deployment_info(self, copier_copy):
        """README.md includes deployment information."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "README.md").read_text()
        assert "Railway" in content

    def test_readme_includes_frontend_when_enabled(self, copier_copy):
        """README.md has frontend section when include_frontend=true."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "README.md").read_text()
        assert "npm" in content


class TestClaudeSettings:
    """Validate generated .claude/settings.json."""

    def test_settings_exists(self, copier_copy):
        """Generated app includes .claude/settings.json."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / ".claude" / "settings.json").exists()

    def test_settings_has_task_list_id(self, copier_copy):
        """settings.json has correct CLAUDE_CODE_TASK_LIST_ID."""
        result_dir = copier_copy(app_name="habit-tracker", include_frontend=False)
        with open(result_dir / ".claude" / "settings.json") as f:
            config = json.load(f)
        assert config["env"]["CLAUDE_CODE_TASK_LIST_ID"] == "habit-tracker"


class TestMemoryScaffold:
    """Validate generated .claude/memory/MEMORY.md."""

    def test_memory_exists(self, copier_copy):
        """Generated app includes memory scaffold."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / ".claude" / "memory" / "MEMORY.md").exists()

    def test_memory_has_section_headers(self, copier_copy):
        """Memory scaffold includes expected section headers."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / ".claude" / "memory" / "MEMORY.md").read_text()
        assert "## Architecture Decisions" in content
        assert "## Debugging Notes" in content
        assert "## Known Gotchas" in content

    def test_memory_contains_app_name(self, copier_copy):
        """Memory scaffold contains the app name."""
        result_dir = copier_copy(app_name="habit-tracker", include_frontend=False)
        content = (result_dir / ".claude" / "memory" / "MEMORY.md").read_text()
        assert "habit-tracker" in content
