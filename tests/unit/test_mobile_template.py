"""Tests for mobile template file rendering."""

import json


class TestAppJson:
    """Test app.json generation."""

    def test_app_json_new_arch_disabled(self, copier_copy):
        """app.json contains newArchEnabled: false."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "app.json") as f:
            config = json.load(f)
        assert config["expo"]["newArchEnabled"] is False

    def test_app_json_bundle_id(self, copier_copy):
        """bundleId matches com.agenticapps.{app_name}."""
        result_dir = copier_copy(app_name="test-app", include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "app.json") as f:
            config = json.load(f)
        assert config["expo"]["ios"]["bundleIdentifier"] == "com.agenticapps.testapp"
        assert config["expo"]["android"]["package"] == "com.agenticapps.testapp"

    def test_app_json_bundle_id_hyphen_stripping(self, copier_copy):
        """app_name with hyphens has them stripped in bundle ID."""
        result_dir = copier_copy(app_name="my-cool-app", include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "app.json") as f:
            config = json.load(f)
        assert config["expo"]["ios"]["bundleIdentifier"] == "com.agenticapps.mycoolapp"

    def test_app_json_scheme(self, copier_copy):
        """scheme is lowercase app_name with hyphens stripped."""
        result_dir = copier_copy(app_name="test-app", include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "app.json") as f:
            config = json.load(f)
        assert config["expo"]["scheme"] == "testapp"


class TestEasJson:
    """Test eas.json generation."""

    def test_eas_json_three_profiles(self, copier_copy):
        """eas.json has development, preview, production profiles."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "eas.json") as f:
            config = json.load(f)
        build = config["build"]
        assert "development" in build
        assert "preview" in build
        assert "production" in build

    def test_eas_json_env_blocks(self, copier_copy):
        """Each profile has EXPO_PUBLIC_* env vars."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "eas.json") as f:
            config = json.load(f)
        for profile_name in ["development", "preview", "production"]:
            env = config["build"][profile_name]["env"]
            assert "EXPO_PUBLIC_API_BASE_URL" in env
            assert "EXPO_PUBLIC_LOGTO_ENDPOINT" in env
            assert "EXPO_PUBLIC_LOGTO_APP_ID" in env
            assert "EXPO_PUBLIC_LOGTO_API_RESOURCE" in env

    def test_eas_json_install_command(self, copier_copy):
        """eas.json base has installCommand for monorepo root (lesson #24)."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "eas.json") as f:
            config = json.load(f)
        assert (
            config["build"]["base"]["installCommand"]
            == "cd .. && pnpm install --no-frozen-lockfile"
        )


class TestMetroConfig:
    """Test metro.config.js generation."""

    def test_metro_config_resolve_request(self, copier_copy):
        """metro.config.js contains resolveRequest override."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        content = (result_dir / "mobile" / "metro.config.js").read_text()
        assert "resolveRequest" in content
        assert "react" in content


class TestMobilePackageJson:
    """Test mobile package.json."""

    def test_package_json_no_banned_packages(self, copier_copy):
        """No react-native-svg, react-native-keyboard-controller, or expo-dev-client."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "package.json") as f:
            pkg = json.load(f)
        all_deps = list(pkg.get("dependencies", {}).keys()) + list(
            pkg.get("devDependencies", {}).keys()
        )
        assert "react-native-svg" not in all_deps
        assert "react-native-keyboard-controller" not in all_deps
        assert "expo-dev-client" not in all_deps

    def test_package_json_expo_sdk_pinned(self, copier_copy):
        """Expo SDK version pinned to ~52.0.0."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "package.json") as f:
            pkg = json.load(f)
        assert pkg["dependencies"]["expo"] == "~52.0.0"

    def test_package_json_babel_runtime(self, copier_copy):
        """@babel/runtime is a direct dependency (lesson #21)."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "package.json") as f:
            pkg = json.load(f)
        assert "@babel/runtime" in pkg["dependencies"]

    def test_package_json_jest_config(self, copier_copy):
        """Jest config has pnpm-aware transformIgnorePatterns (lesson #23)."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        with open(result_dir / "mobile" / "package.json") as f:
            pkg = json.load(f)
        assert "jest" in pkg
        patterns = pkg["jest"]["transformIgnorePatterns"]
        assert any(".pnpm" in p for p in patterns)


class TestLayoutFiles:
    """Test app layout files."""

    def test_layout_useeffect_splash(self, copier_copy):
        """_layout.tsx uses useEffect with font guard for SplashScreen.hideAsync (lesson #4)."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        content = (result_dir / "mobile" / "app" / "_layout.tsx").read_text()
        assert "useEffect" in content
        assert "hideAsync" in content
        assert "onLayout=" not in content
        # Font guard pattern (lesson #4) — splash must wait for fonts
        assert "fontsLoaded" in content
        assert "fontError" in content
        assert "useFonts" in content

    def test_layout_weakref_polyfill(self, copier_copy):
        """_layout.tsx contains WeakRef polyfill with guard."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        content = (result_dir / "mobile" / "app" / "_layout.tsx").read_text()
        assert "typeof WeakRef" in content
        assert "globalThis" in content


class TestComponents:
    """Test component files."""

    def test_authgate_token_dedup(self, copier_copy):
        """AuthGate.tsx contains pendingToken dedup logic."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        content = (result_dir / "mobile" / "components" / "AuthGate.tsx").read_text()
        assert "pendingToken" in content

    def test_keyboard_wrapper_offset_zero(self, copier_copy):
        """KeyboardWrapper uses keyboardVerticalOffset={0}."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        content = (result_dir / "mobile" / "components" / "KeyboardWrapper.tsx").read_text()
        assert "keyboardVerticalOffset={0}" in content


class TestEasignore:
    """Test .easignore generation."""

    def test_easignore_leading_slashes(self, copier_copy):
        """All root-level patterns start with /."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        content = (result_dir / ".easignore").read_text()
        assert "/app/" in content
        assert "/tests/" in content
        assert "/.venv/" in content
        assert "/frontend/" in content

    def test_easignore_no_bare_app(self, copier_copy):
        """.easignore does NOT contain bare 'app/' (without leading slash)."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        content = (result_dir / ".easignore").read_text()
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                if "app/" in line:
                    assert line.startswith("/") or line.startswith("*"), \
                        f"Bare 'app/' pattern found: '{line}' — must use leading slash"

    def test_easignore_not_generated_when_no_mobile(self, copier_copy):
        """.easignore NOT generated when include_mobile=false."""
        result_dir = copier_copy(include_frontend=True, include_mobile=False)
        assert not (result_dir / ".easignore").exists()


class TestMobileConditional:
    """Test mobile directory conditional rendering."""

    def test_mobile_not_generated_when_disabled(self, copier_copy):
        """include_mobile=false -> no mobile/ directory."""
        result_dir = copier_copy(include_frontend=True, include_mobile=False)
        assert not (result_dir / "mobile").exists()

    def test_no_jinja_remnants_in_mobile(self, copier_copy):
        """No Jinja2 syntax remnants in any mobile file."""
        result_dir = copier_copy(include_frontend=True, include_mobile=True)
        mobile_dir = result_dir / "mobile"
        for f in mobile_dir.rglob("*"):
            if f.is_file() and f.suffix in (".tsx", ".ts", ".json", ".js"):
                content = f.read_text()
                assert "{{" not in content, f"Jinja remnant in {f.relative_to(result_dir)}"
                assert "{%" not in content, f"Jinja remnant in {f.relative_to(result_dir)}"
