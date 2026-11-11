"""Tests for repo hygiene: .env consolidation at root."""

from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent.parent


class TestRootEnvExample:
    """Verify .env.example exists at root with correct variable names."""

    def test_env_example_exists(self):
        env_example = REPO_ROOT / ".env.example"
        assert env_example.exists(), "Root .env.example should exist"

    def test_env_example_has_required_vars(self):
        env_example = REPO_ROOT / ".env.example"
        content = env_example.read_text()
        required_vars = [
            "LOGTO_ENDPOINT",
            "LOGTO_M2M_APP_ID",
            "LOGTO_M2M_APP_SECRET",
            "LOGTO_API_RESOURCE",
            "RAILWAY_API_TOKEN",
        ]
        for var in required_vars:
            assert var in content, f".env.example should contain {var}"

    def test_env_example_has_no_real_secrets(self):
        """Ensure .env.example doesn't contain actual credentials."""
        env_example = REPO_ROOT / ".env.example"
        content = env_example.read_text()
        # Parse non-comment lines with = assignments
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                # Secret-containing vars should have placeholder values
                if "SECRET" in key:
                    assert value.startswith("your-"), (
                        f"{key} should have a placeholder value, not a real secret"
                    )
                if "APP_ID" in key and "M2M" in key:
                    assert value.startswith("your-"), (
                        f"{key} should have a placeholder value"
                    )


class TestGitignore:
    """Verify .gitignore covers .env at root."""

    def test_gitignore_covers_env(self):
        gitignore = REPO_ROOT / ".gitignore"
        content = gitignore.read_text()
        # .env should be in gitignore (either as '.env' or '*.env' or similar)
        lines = [line.strip() for line in content.splitlines() if not line.startswith("#")]
        assert ".env" in lines, ".gitignore should contain .env"


class TestVerifyLogtoUpdated:
    """Verify verify-logto scripts point to root .env."""

    def test_utils_loads_from_root(self):
        utils_path = REPO_ROOT / "scripts" / "verify-logto" / "utils.py"
        content = utils_path.read_text()
        # Should reference parent.parent.parent (repo root), not same directory
        assert "parent.parent.parent" in content or "_repo_root" in content, (
            "utils.py should load .env from repo root, not scripts/verify-logto/"
        )

    def test_utils_uses_new_var_names(self):
        utils_path = REPO_ROOT / "scripts" / "verify-logto" / "utils.py"
        content = utils_path.read_text()
        assert "LOGTO_M2M_APP_ID" in content, "utils.py should use LOGTO_M2M_APP_ID"
        assert "LOGTO_M2M_APP_SECRET" in content, "utils.py should use LOGTO_M2M_APP_SECRET"
        assert "LOGTO_API_RESOURCE" in content, "utils.py should use LOGTO_API_RESOURCE"
        # Old names should not be used as get_env keys
        assert 'get_env("M2M_CLIENT_ID")' not in content, (
            "utils.py should not use old M2M_CLIENT_ID key"
        )
        assert 'get_env("M2M_CLIENT_SECRET")' not in content, (
            "utils.py should not use old M2M_CLIENT_SECRET key"
        )

    def test_old_env_file_deprecated(self):
        old_env = REPO_ROOT / "scripts" / "verify-logto" / ".env"
        if old_env.exists():
            content = old_env.read_text()
            # Should not contain real credentials — either deprecated notice or removed
            assert "DEPRECATED" in content.upper() or "moved" in content.lower(), (
                "Old .env should contain deprecation notice"
            )
            # Should not contain actual secret values
            assert "M2M_CLIENT_SECRET=" not in content or "your-" in content, (
                "Old .env should not contain real secrets"
            )

    def test_old_env_example_points_to_root(self):
        old_example = REPO_ROOT / "scripts" / "verify-logto" / ".env.example"
        content = old_example.read_text()
        assert "root" in content.lower() or "../../" in content, (
            "Old .env.example should point users to root .env"
        )
