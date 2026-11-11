"""Tests for generated CLAUDE.md, README.md, and .claude/ scaffolding."""

import json


class TestClaudeMd:
    """Validate generated CLAUDE.md."""

    def test_claude_md_exists(self, copier_copy):
        """Generated app includes CLAUDE.md."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "CLAUDE.md").exists()

    def test_claude_md_contains_app_name(self, copier_copy):
        """CLAUDE.md contains the app name."""
        result_dir = copier_copy(app_name="habit-tracker", include_frontend=False)
        content = (result_dir / "CLAUDE.md").read_text()
        assert "habit-tracker" in content

    def test_claude_md_contains_description(self, copier_copy):
        """CLAUDE.md contains the app description."""
        result_dir = copier_copy(
            app_description="Track daily habits", include_frontend=False
        )
        content = (result_dir / "CLAUDE.md").read_text()
        assert "Track daily habits" in content

    def test_claude_md_has_commands(self, copier_copy):
        """CLAUDE.md includes build/test/lint commands."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "CLAUDE.md").read_text()
        assert "pytest" in content
        assert "ruff check" in content
        assert "uvicorn" in content
        assert "alembic" in content

    def test_claude_md_has_architecture(self, copier_copy):
        """CLAUDE.md includes architecture overview."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "CLAUDE.md").read_text()
        assert "app/routes/" in content
        assert "app/models/" in content
        assert "app/services/" in content
        assert "app/db/" in content
        assert "app/auth/" in content

    def test_claude_md_under_line_limit(self, copier_copy):
        """CLAUDE.md stays concise — backend-only <100 lines, with-frontend
        <130 (deployment lessons add ~45 lines)."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "CLAUDE.md").read_text()
        line_count = len(content.strip().split("\n"))
        assert line_count < 100, f"CLAUDE.md (backend-only) is {line_count} lines (should be <100)"

    def test_render_target_claude_md_under_line_limit(self, copier_copy):
        """Render-target CLAUDE.md stays under 100 lines with the frontend on
        and mobile off (the default Render shape)."""
        result_dir = copier_copy(
            include_frontend=True, include_mobile=False, deploy_target="render"
        )
        content = (result_dir / "CLAUDE.md").read_text()
        line_count = len(content.strip().split("\n"))
        assert line_count < 100, (
            f"CLAUDE.md (render, frontend) is {line_count} lines (should be <100)"
        )

    def test_claude_md_includes_frontend_when_enabled(self, copier_copy):
        """CLAUDE.md mentions frontend tech when include_frontend=true."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "CLAUDE.md").read_text()
        assert "Vite" in content or "React" in content or "frontend" in content

    def test_claude_md_no_frontend_when_disabled(self, copier_copy):
        """CLAUDE.md does not mention frontend dir when include_frontend=false."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "CLAUDE.md").read_text()
        assert "| `frontend/`" not in content


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
