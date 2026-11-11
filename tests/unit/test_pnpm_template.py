"""Tests for pnpm workspace and root config template rendering."""

import json

import yaml


class TestRootPackageJson:
    """Test root package.json generation."""

    def test_root_package_json_exists(self, copier_copy):
        """Root package.json generated when include_frontend=true."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "package.json").exists()

    def test_root_package_json_not_generated_when_no_frontend(self, copier_copy):
        """Root package.json NOT generated when include_frontend=false."""
        result_dir = copier_copy(include_frontend=False)
        assert not (result_dir / "package.json").exists()

    def test_pnpm_overrides(self, copier_copy):
        """Root package.json has pnpm.overrides section for CVE packages."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "package.json") as f:
            pkg = json.load(f)
        assert "pnpm" in pkg
        assert "overrides" in pkg["pnpm"]
        assert "react-server-dom-webpack" in pkg["pnpm"]["overrides"]

    def test_root_package_json_is_private(self, copier_copy):
        """Root package.json has private: true."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "package.json") as f:
            pkg = json.load(f)
        assert pkg["private"] is True

    def test_root_package_json_has_app_name(self, copier_copy):
        """Root package.json name includes app name."""
        result_dir = copier_copy(app_name="my-cool-app", include_frontend=True)
        with open(result_dir / "package.json") as f:
            pkg = json.load(f)
        assert "my-cool-app" in pkg["name"]


class TestPnpmWorkspace:
    """Test pnpm-workspace.yaml generation."""

    def test_pnpm_workspace_yaml(self, copier_copy):
        """pnpm-workspace.yaml exists when include_frontend=true."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "pnpm-workspace.yaml").exists()

    def test_workspace_includes_mobile(self, copier_copy):
        """When include_mobile=true, workspace includes mobile."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        with open(result_dir / "pnpm-workspace.yaml") as f:
            ws = yaml.safe_load(f)
        assert "mobile" in ws["packages"]
        assert "frontend" in ws["packages"]
        assert "shared" in ws["packages"]

    def test_workspace_excludes_mobile_when_disabled(self, copier_copy):
        """When include_mobile=false, workspace does NOT include mobile."""
        result_dir = copier_copy(include_frontend=True, include_mobile=False)
        with open(result_dir / "pnpm-workspace.yaml") as f:
            ws = yaml.safe_load(f)
        assert "mobile" not in ws["packages"]
        assert "frontend" in ws["packages"]
        assert "shared" in ws["packages"]

    def test_no_pnpm_workspace_when_no_frontend(self, copier_copy):
        """When include_frontend=false, no pnpm-workspace.yaml generated."""
        result_dir = copier_copy(include_frontend=False)
        assert not (result_dir / "pnpm-workspace.yaml").exists()


class TestNpmrc:
    """Test .npmrc generation."""

    def test_npmrc_hoisted(self, copier_copy):
        """`.npmrc` contains node-linker=hoisted."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / ".npmrc").read_text()
        assert "node-linker=hoisted" in content

    def test_npmrc_not_generated_when_no_frontend(self, copier_copy):
        """`.npmrc` NOT generated when include_frontend=false."""
        result_dir = copier_copy(include_frontend=False)
        assert not (result_dir / ".npmrc").exists()


class TestDockerfile:
    """Test Dockerfile pnpm integration."""

    def test_dockerfile_uses_pnpm(self, copier_copy):
        """Dockerfile contains pnpm install --no-frozen-lockfile."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "Dockerfile").read_text()
        assert "pnpm install --no-frozen-lockfile" in content

    def test_dockerfile_no_npm(self, copier_copy):
        """Dockerfile does NOT contain npm ci or npm install."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "Dockerfile").read_text()
        assert "npm ci" not in content
        assert "npm install" not in content or "npm i -g pnpm" in content

    def test_dockerfile_copies_shared(self, copier_copy):
        """Dockerfile copies shared/ into build stage."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "Dockerfile").read_text()
        assert "shared/" in content

    def test_dockerfile_no_frontend_stage_when_disabled(self, copier_copy):
        """Backend-only Dockerfile has no frontend build stage."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "Dockerfile").read_text()
        assert "frontend-build" not in content
        assert "pnpm" not in content


class TestGitignore:
    """Test .gitignore updates."""

    def test_gitignore_package_lock(self, copier_copy):
        """.gitignore includes package-lock.json."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / ".gitignore").read_text()
        assert "package-lock.json" in content

    def test_gitignore_mobile_env(self, copier_copy):
        """.gitignore includes mobile/.env."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / ".gitignore").read_text()
        assert "mobile/.env" in content
