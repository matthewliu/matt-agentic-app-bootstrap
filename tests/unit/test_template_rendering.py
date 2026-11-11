"""Tests for template rendering with different variable combinations."""

import re
import subprocess


class TestRenderDefault:
    """Test rendering with default (no frontend) variables."""

    def test_auth_middleware_exists(self, copier_copy):
        """Generated app includes auth middleware."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "app" / "auth" / "middleware.py").exists()

    def test_auth_middleware_compiles(self, copier_copy):
        """Generated auth middleware is valid Python."""
        result_dir = copier_copy(include_frontend=False)
        middleware = result_dir / "app" / "auth" / "middleware.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(middleware)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"middleware.py compile error: {result.stderr}"

    def test_auth_middleware_has_validate_token(self, copier_copy):
        """Generated middleware includes validate_token function."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "auth" / "middleware.py").read_text()
        assert "async def validate_token" in content

    def test_auth_middleware_has_require_scope(self, copier_copy):
        """Generated middleware includes require_scope factory."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "auth" / "middleware.py").read_text()
        assert "def require_scope" in content

    def test_auth_middleware_uses_es384(self, copier_copy):
        """Generated middleware uses ES384 algorithm."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "auth" / "middleware.py").read_text()
        assert "ES384" in content

    def test_auth_middleware_uses_certifi(self, copier_copy):
        """Generated middleware uses certifi for SSL."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "auth" / "middleware.py").read_text()
        assert "certifi" in content

    def test_db_engine_exists(self, copier_copy):
        """Generated app includes db engine."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "app" / "db" / "engine.py").exists()

    def test_db_engine_compiles(self, copier_copy):
        """Generated db engine is valid Python."""
        result_dir = copier_copy(include_frontend=False)
        engine = result_dir / "app" / "db" / "engine.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(engine)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"engine.py compile error: {result.stderr}"

    def test_db_engine_has_get_db(self, copier_copy):
        """Generated engine includes get_db dependency."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "db" / "engine.py").read_text()
        assert "async def get_db" in content

    def test_db_models_exists(self, copier_copy):
        """Generated app includes db models with Item."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "app" / "db" / "models.py").exists()

    def test_db_models_compiles(self, copier_copy):
        """Generated db models is valid Python."""
        result_dir = copier_copy(include_frontend=False)
        models = result_dir / "app" / "db" / "models.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(models)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"models.py compile error: {result.stderr}"

    def test_db_models_has_item(self, copier_copy):
        """Generated models include Item class."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "db" / "models.py").read_text()
        assert "class Item" in content

    def test_alembic_ini_exists(self, copier_copy):
        """Generated app includes alembic.ini."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "app" / "db" / "migrations" / "alembic.ini").exists()

    def test_alembic_env_exists(self, copier_copy):
        """Generated app includes alembic env.py."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "app" / "db" / "migrations" / "env.py").exists()

    def test_alembic_env_compiles(self, copier_copy):
        """Generated alembic env.py is valid Python."""
        result_dir = copier_copy(include_frontend=False)
        env_py = result_dir / "app" / "db" / "migrations" / "env.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(env_py)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"env.py compile error: {result.stderr}"

    def test_initial_migration_exists(self, copier_copy):
        """Generated app includes initial migration."""
        result_dir = copier_copy(include_frontend=False)
        migration = result_dir / "app" / "db" / "migrations" / "versions" / "001_initial.py"
        assert migration.exists()

    def test_initial_migration_compiles(self, copier_copy):
        """Generated initial migration is valid Python."""
        result_dir = copier_copy(include_frontend=False)
        migration = result_dir / "app" / "db" / "migrations" / "versions" / "001_initial.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(migration)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"001_initial.py compile error: {result.stderr}"

    def test_initial_migration_creates_items_table(self, copier_copy):
        """Initial migration creates the items table."""
        result_dir = copier_copy(include_frontend=False)
        migration = result_dir / "app" / "db" / "migrations" / "versions" / "001_initial.py"
        content = migration.read_text()
        assert '"items"' in content
        assert "def upgrade" in content
        assert "def downgrade" in content

    def test_no_jinja_remnants_all_files(self, copier_copy):
        """No Jinja2 syntax remnants in any generated file."""
        result_dir = copier_copy(include_frontend=False)
        for f in result_dir.rglob("*"):
            if f.is_file() and f.suffix in (".py", ".ini", ".yml", ".yaml"):
                content = f.read_text()
                assert "{{" not in content, f"Jinja remnant in {f.relative_to(result_dir)}"
                assert "{%" not in content, f"Jinja remnant in {f.relative_to(result_dir)}"

    def test_logto_endpoint_from_settings(self, copier_copy):
        """Auth middleware reads Logto config from settings object, not os.environ."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "auth" / "middleware.py").read_text()
        assert "settings.logto_endpoint" in content
        assert "settings.logto_api_resource" in content


class TestCrudResource:
    """Test sample CRUD resource template files."""

    def test_items_router_compiles(self, copier_copy):
        """Generated items.py is valid Python."""
        result_dir = copier_copy(include_frontend=False)
        items_py = result_dir / "app" / "routes" / "items.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(items_py)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"items.py compile error: {result.stderr}"

    def test_items_router_has_crud_endpoints(self, copier_copy):
        """Generated items router has all CRUD operations."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "routes" / "items.py").read_text()
        assert "async def list_items" in content
        assert "async def create_item" in content
        assert "async def get_item" in content
        assert "async def update_item" in content
        assert "async def delete_item" in content

    def test_items_router_has_auth(self, copier_copy):
        """All CRUD routes require auth via get_or_create_user_profile."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "routes" / "items.py").read_text()
        assert "get_or_create_user_profile" in content
        assert "Depends(get_or_create_user_profile)" in content

    def test_items_router_has_correct_status_codes(self, copier_copy):
        """CRUD routes use correct HTTP status codes."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "routes" / "items.py").read_text()
        assert "status_code=201" in content  # create
        assert "status_code=204" in content  # delete

    def test_pydantic_models_exist(self, copier_copy):
        """Generated app includes Pydantic item models."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "app" / "models" / "item.py").exists()

    def test_pydantic_models_compile(self, copier_copy):
        """Generated Pydantic models are valid Python."""
        result_dir = copier_copy(include_frontend=False)
        item_py = result_dir / "app" / "models" / "item.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(item_py)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"item.py compile error: {result.stderr}"

    def test_pydantic_models_has_classes(self, copier_copy):
        """Pydantic models include Create, Update, and Response classes."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "models" / "item.py").read_text()
        assert "class ItemCreate" in content
        assert "class ItemUpdate" in content
        assert "class ItemResponse" in content

    def test_service_exists(self, copier_copy):
        """Generated app includes item service."""
        result_dir = copier_copy(include_frontend=False)
        assert (result_dir / "app" / "services" / "item_service.py").exists()

    def test_service_compiles(self, copier_copy):
        """Generated item service is valid Python."""
        result_dir = copier_copy(include_frontend=False)
        service = result_dir / "app" / "services" / "item_service.py"
        result = subprocess.run(
            ["python3", "-m", "py_compile", str(service)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"item_service.py compile error: {result.stderr}"

    def test_service_has_crud_functions(self, copier_copy):
        """Service layer has all CRUD functions."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "services" / "item_service.py").read_text()
        assert "async def create_item" in content
        assert "async def get_item" in content
        assert "async def get_items" in content
        assert "async def update_item" in content
        assert "async def delete_item" in content

    def test_service_uses_async_session(self, copier_copy):
        """Service functions use AsyncSession."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "services" / "item_service.py").read_text()
        assert "AsyncSession" in content

    def test_item_service_accepts_user_id(self, copier_copy):
        """Item service methods accept user_id for per-user scoping."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "services" / "item_service.py").read_text()
        assert "user_id: str" in content
        assert "Item.user_id == user_id" in content


class TestUserProfileTemplate:
    """Tests for UserProfile-related template files."""

    def test_user_profile_orm_model(self, copier_copy):
        """models.py contains UserProfile with correct fields."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "db" / "models.py").read_text()
        assert "class UserProfile(Base)" in content
        assert "user_id" in content
        assert "timezone" in content
        assert "created_at" in content
        assert "updated_at" in content
        # No seed_version field
        assert "seed_version" not in content

    def test_user_profile_pydantic_schemas(self, copier_copy):
        """user_profile.py has Pydantic schemas."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "models" / "user_profile.py").read_text()
        assert "class UserProfileResponse(BaseModel)" in content
        assert "class UserProfileUpdate(BaseModel)" in content
        assert "zoneinfo.ZoneInfo" in content

    def test_dependencies_exists(self, copier_copy):
        """dependencies.py exists with get_or_create_user_profile."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "auth" / "dependencies.py").read_text()
        assert "get_or_create_user_profile" in content
        assert "X-Timezone" not in content or "x_timezone" in content

    def test_user_profile_service_exists(self, copier_copy):
        """user_profile_service.py exists with IntegrityError handling."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "services" / "user_profile_service.py").read_text()
        assert "IntegrityError" in content
        assert "get_or_create_profile" in content

    def test_middleware_has_get_current_user_id(self, copier_copy):
        """middleware.py has get_current_user_id helper."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "auth" / "middleware.py").read_text()
        assert "def get_current_user_id" in content
        assert 'claims["sub"]' in content

    def test_migration_has_user_profiles(self, copier_copy):
        """Initial migration creates user_profiles table."""
        result_dir = copier_copy(include_frontend=False)
        migration = (
            result_dir / "app" / "db" / "migrations" / "versions" / "001_initial.py"
        )
        content = migration.read_text()
        assert '"user_profiles"' in content
        assert '"user_id"' in content

    def test_main_has_x_timezone_cors(self, copier_copy):
        """main.py allows X-Timezone in CORS headers."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "main.py").read_text()
        assert "X-Timezone" in content

    def test_main_has_generic_500_handler(self, copier_copy):
        """main.py has a generic 500 exception handler."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "main.py").read_text()
        assert "generic_exception_handler" in content
        assert "Internal server error" in content


class TestFrontendScaffold:
    """Tests for frontend auth and design system template files."""

    def test_app_tsx_has_router(self, copier_copy):
        """App.tsx contains BrowserRouter and route definitions."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "src" / "App.tsx").read_text()
        assert "BrowserRouter" in content
        assert "Routes" in content
        assert "LoginPage" in content
        assert "CallbackPage" in content
        assert "ProtectedLayout" in content

    def test_layout_has_auth_guard(self, copier_copy):
        """Layout.tsx has auth guard with useLogto and useRef."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "src" / "components" / "Layout.tsx").read_text()
        assert "useLogto" in content
        assert "isAuthenticated" in content
        assert "signOut" in content
        assert "useRef" in content
        assert "wasAuthenticated" in content
        assert "Outlet" in content

    def test_config_ts_has_runtime_guard(self, copier_copy):
        """config.ts reports missing VITE_* vars as a not-configured result."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "src" / "config.ts").read_text()
        assert "VITE_LOGTO_ENDPOINT" in content
        assert "VITE_LOGTO_APP_ID" in content
        assert "configured: false" in content
        assert "getLogtoConfig" in content

    def test_useapi_has_timezone(self, copier_copy):
        """useApi.ts sends X-Timezone header."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "src" / "hooks" / "useApi.ts").read_text()
        assert "X-Timezone" in content
        assert "Authorization" in content
        assert "Bearer" in content

    def test_login_page_exists(self, copier_copy):
        """LoginPage.tsx exists with sign-in button."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "src" / "pages" / "LoginPage.tsx").read_text()
        assert "signIn" in content
        assert "Sign In" in content

    def test_callback_page_has_error_state(self, copier_copy):
        """CallbackPage.tsx handles errors with retry link."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "frontend" / "src" / "pages" / "CallbackPage.tsx").read_text()
        assert "useHandleSignInCallback" in content
        assert "error" in content
        assert "Try again" in content

    def test_design_tokens_in_tailwind(self, copier_copy):
        """tailwind.config.ts reads every design value from src/design/tokens.ts."""
        result_dir = copier_copy(include_frontend=True)
        frontend = result_dir / "frontend"
        config = (frontend / "tailwind.config.ts").read_text()
        assert 'from "./src/design/tokens"' in config
        assert "var(--color-${name})" in config
        tokens = (frontend / "src" / "design" / "tokens.ts").read_text()
        assert '"bg-primary"' in tokens
        assert "accent" in tokens
        assert "Urbanist" in tokens
        assert "Figtree" in tokens

    def test_css_has_dark_and_light_themes(self, copier_copy):
        """Tokens hold a light and a dark value; the config applies the OS
        setting and a manual data-theme override; index.css holds no colors."""
        result_dir = copier_copy(include_frontend=True)
        frontend = result_dir / "frontend"
        tokens = (frontend / "src" / "design" / "tokens.ts").read_text()
        assert "#0a0a0f" in tokens  # dark bg
        assert "#fafafa" in tokens  # light bg
        config = (frontend / "tailwind.config.ts").read_text()
        assert "prefers-color-scheme: dark" in config
        assert ':root:not([data-theme="light"])' in config
        assert ':root[data-theme="dark"]' in config
        assert "prefers-reduced-motion: reduce" in config
        css = (frontend / "src" / "index.css").read_text()
        assert re.search(r"#[0-9a-fA-F]{3,8}\b", css) is None, "colors belong in tokens.ts"

    def test_google_fonts_in_html(self, copier_copy):
        """The font URL lives in tokens.ts; vite.config.ts injects it into index.html."""
        result_dir = copier_copy(include_frontend=True)
        frontend = result_dir / "frontend"
        tokens = (frontend / "src" / "design" / "tokens.ts").read_text()
        assert "export const fontStylesheet" in tokens
        assert "fonts.googleapis.com" in tokens and "Urbanist" in tokens
        vite = (frontend / "vite.config.ts").read_text()
        assert 'import { fontStylesheet } from "./src/design/tokens";' in vite
        assert "preconnect" in vite and "designTokenFonts()" in vite
        assert "fonts.googleapis.com/css2" not in (frontend / "index.html").read_text()

    def test_no_frontend_excludes_frontend_files(self, copier_copy):
        """include_frontend=false excludes all frontend files."""
        result_dir = copier_copy(include_frontend=False)
        assert not (result_dir / "frontend").exists()
        # Backend files should still exist
        assert (result_dir / "app" / "auth" / "dependencies.py").exists()
        assert (result_dir / "app" / "services" / "user_profile_service.py").exists()


class TestSpaRouting:
    """Tests for SPA fallback routing in main.py template."""

    def test_spa_catchall_route_present(self, copier_copy):
        """Generated main.py contains catch-all route for SPA routing."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "app" / "main.py").read_text()
        assert "/{path:path}" in content
        assert "serve_spa" in content

    def test_spa_path_traversal_protection(self, copier_copy):
        """Generated main.py has resolve() + is_relative_to() path containment check."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "app" / "main.py").read_text()
        assert ".resolve()" in content
        assert "is_relative_to(" in content

    def test_no_staticfiles_html_true(self, copier_copy):
        """Generated main.py does NOT use StaticFiles(html=True) mount at root."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "app" / "main.py").read_text()
        assert 'html=True' not in content

    def test_assets_mount_conditional(self, copier_copy):
        """Generated main.py mounts /assets conditionally on directory existing."""
        result_dir = copier_copy(include_frontend=True)
        content = (result_dir / "app" / "main.py").read_text()
        assert '"/assets"' in content
        assert "_assets_dir.is_dir()" in content

    def test_no_spa_routes_without_frontend(self, copier_copy):
        """When include_frontend=false, no SPA routes in main.py."""
        result_dir = copier_copy(include_frontend=False)
        content = (result_dir / "app" / "main.py").read_text()
        assert "serve_spa" not in content
        assert "/{path:path}" not in content
