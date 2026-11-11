"""Integration tests for cross-sub-feature mobile template flows."""

import json
import re

import yaml


class TestFullFlowWithMobile:
    """Test complete template rendering with mobile enabled."""

    def test_full_flow_with_mobile(self, copier_copy):
        """Copier render with include_frontend=true, include_mobile=true produces complete app."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)

        # mobile/ exists with config files
        assert (result_dir / "mobile").is_dir()
        assert (result_dir / "mobile" / "app.json").exists()
        assert (result_dir / "mobile" / "eas.json").exists()
        assert (result_dir / "mobile" / "metro.config.js").exists()
        assert (result_dir / "mobile" / "package.json").exists()
        assert (result_dir / "mobile" / "app" / "_layout.tsx").exists()

        # shared/ exists
        assert (result_dir / "shared").is_dir()
        assert (result_dir / "shared" / "package.json").exists()
        assert (result_dir / "shared" / "src" / "types" / "api.ts").exists()

        # pnpm workspace correct
        with open(result_dir / "pnpm-workspace.yaml") as f:
            ws = yaml.safe_load(f)
        assert set(ws["packages"]) == {"frontend", "mobile", "shared"}

        # .npmrc
        assert "node-linker=hoisted" in (result_dir / ".npmrc").read_text()

        # .easignore has leading slashes
        easignore = (result_dir / ".easignore").read_text()
        assert "/app/" in easignore
        assert "/tests/" in easignore

        # Dockerfile uses pnpm
        dockerfile = (result_dir / "Dockerfile").read_text()
        assert "pnpm install --no-frozen-lockfile" in dockerfile


class TestFullFlowWithoutMobile:
    """Test template rendering with mobile disabled but frontend enabled."""

    def test_full_flow_without_mobile(self, copier_copy):
        """include_frontend=true, include_mobile=false: no mobile/, shared/ still exists."""
        result_dir = copier_copy(include_frontend=True, include_mobile=False)

        assert not (result_dir / "mobile").exists()
        assert (result_dir / "shared").is_dir()
        assert (result_dir / "frontend").is_dir()

        with open(result_dir / "pnpm-workspace.yaml") as f:
            ws = yaml.safe_load(f)
        assert "mobile" not in ws["packages"]
        assert "frontend" in ws["packages"]
        assert "shared" in ws["packages"]

        # No .easignore when mobile is off
        assert not (result_dir / ".easignore").exists()

        # Dockerfile still uses pnpm
        dockerfile = (result_dir / "Dockerfile").read_text()
        assert "pnpm install --no-frozen-lockfile" in dockerfile


class TestNoFrontendImpliesNoMobile:
    """Test that --no-frontend skips both frontend and mobile."""

    def test_no_frontend_implies_no_mobile(self, copier_copy):
        """include_frontend=false: no frontend/, no mobile/, no shared/, no pnpm files."""
        result_dir = copier_copy(include_frontend=False)

        assert not (result_dir / "frontend").exists()
        assert not (result_dir / "mobile").exists()
        assert not (result_dir / "shared").exists()
        assert not (result_dir / "pnpm-workspace.yaml").exists()
        assert not (result_dir / ".npmrc").exists()
        assert not (result_dir / "package.json").exists()

        # Dockerfile has no frontend build stage
        dockerfile = (result_dir / "Dockerfile").read_text()
        assert "frontend-build" not in dockerfile
        assert "pnpm" not in dockerfile


class TestNoSecretsInTemplate:
    """Test that no secrets are hardcoded in generated files."""

    def test_no_secrets_in_template(self, copier_copy):
        """Scan all generated files for hardcoded secrets."""
        result_dir = copier_copy(
            include_frontend=True,
            include_mobile=True,
            logto_endpoint="https://logto.test.example.com",
        )
        secret_patterns = [
            r"sk_[a-zA-Z0-9]{20,}",  # API keys
            r"[a-f0-9]{32,}",  # hex tokens (skip short ones)
        ]
        for f in result_dir.rglob("*"):
            if f.is_file() and f.suffix in (".ts", ".tsx", ".json", ".js", ".py", ".env"):
                content = f.read_text()
                for pattern in secret_patterns:
                    matches = re.findall(pattern, content)
                    for match in matches:
                        # Allow known non-secret hex values (colors, etc.)
                        if len(match) > 40:
                            raise AssertionError(
                                f"Possible secret in "
                                f"{f.relative_to(result_dir)}: {match[:20]}..."
                            )


class TestBundleIdFormat:
    """Test bundle ID format validation."""

    def test_bundle_id_format(self, copier_copy):
        """Bundle ID is well-formed com.agenticapps.{sanitized_name}."""
        result_dir = copier_copy(
            app_name="my-test-app",
            include_frontend=True,
            include_mobile=True,
        )
        with open(result_dir / "mobile" / "app.json") as f:
            config = json.load(f)
        bundle_id = config["expo"]["ios"]["bundleIdentifier"]
        assert re.match(r"^com\.agenticapps\.[a-z0-9]+$", bundle_id), (
            f"Invalid bundle ID: {bundle_id}"
        )


class TestGeneratedJsonValid:
    """Test that all generated JSON files are valid."""

    def test_generated_json_valid(self, copier_copy):
        """Parse app.json, eas.json, root package.json as valid JSON."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)

        json_files = [
            result_dir / "package.json",
            result_dir / "mobile" / "app.json",
            result_dir / "mobile" / "eas.json",
            result_dir / "mobile" / "package.json",
            result_dir / "shared" / "package.json",
            result_dir / "frontend" / "package.json",
        ]

        for jf in json_files:
            assert jf.exists(), f"JSON file missing: {jf.relative_to(result_dir)}"
            with open(jf) as f:
                data = json.load(f)
            assert isinstance(data, dict), f"Not a JSON object: {jf.relative_to(result_dir)}"
