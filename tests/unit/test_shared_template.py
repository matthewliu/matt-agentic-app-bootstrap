"""Tests for shared package template rendering."""

import json


class TestSharedPackageJson:
    """Test shared/package.json generation."""

    def test_shared_package_json_peer_deps(self, copier_copy):
        """react is in peerDependencies, not dependencies."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "shared" / "package.json") as f:
            pkg = json.load(f)
        assert "react" in pkg["peerDependencies"]
        assert "dependencies" not in pkg or "react" not in pkg.get("dependencies", {})

    def test_shared_exports_types(self, copier_copy):
        """package.json exports ./types."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "shared" / "package.json") as f:
            pkg = json.load(f)
        assert "./types" in pkg["exports"]

    def test_shared_exports_api_client(self, copier_copy):
        """package.json exports ./api-client."""
        result_dir = copier_copy(include_frontend=True)
        with open(result_dir / "shared" / "package.json") as f:
            pkg = json.load(f)
        assert "./api-client" in pkg["exports"]


class TestSharedTypes:
    """Test shared type definitions."""

    def test_api_types_match_pydantic(self, copier_copy):
        """api.ts has Item and UserProfile interfaces."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "shared" / "src" / "types" / "api.ts").read_text()
        assert "interface Item" in content
        assert "interface UserProfile" in content

    def test_api_client_no_auth(self, copier_copy):
        """api-client.ts does NOT import auth libraries."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "shared" / "src" / "api-client.ts").read_text()
        assert "@logto" not in content
        # Check that no import statement references auth modules
        for line in content.splitlines():
            if line.strip().startswith("import"):
                assert "auth" not in line.lower(), f"Auth import found: {line}"

    def test_no_jinja_remnants_in_shared(self, copier_copy):
        """No Jinja2 syntax remnants in any shared file."""
        result_dir = copier_copy(include_frontend=True)
        shared_dir = result_dir / "shared"
        for f in shared_dir.rglob("*"):
            if f.is_file() and f.suffix in (".ts", ".json"):
                content = f.read_text()
                assert "{{" not in content, f"Jinja remnant in {f.relative_to(result_dir)}"
                assert "{%" not in content, f"Jinja remnant in {f.relative_to(result_dir)}"


class TestSharedConditional:
    """Test shared directory conditional rendering."""

    def test_shared_exists_when_frontend_on(self, copier_copy):
        """shared/ exists when include_frontend=true."""
        result_dir = copier_copy(include_frontend=True)
        assert (result_dir / "shared").exists()

    def test_shared_not_generated_when_no_frontend(self, copier_copy):
        """shared/ NOT generated when include_frontend=false."""
        result_dir = copier_copy(include_frontend=False)
        assert not (result_dir / "shared").exists()
