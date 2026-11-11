"""Rendering tests for the generated CLI operations, items commands, and MCP server.

These check what each template variant renders. Behavior is tested inside
the generated app (``tests/unit/test_cli_operations.py`` and
``tests/unit/test_mcp_server.py`` in the template), which
``tests/integration/test_generated_app.py`` runs.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from agentic_core.bootstrap import template as template_mod

REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"
APP = "mcp-app"
APP_UPPER = "MCP_APP"


def render(tmp_path: Path, variant: str = "full", **extra) -> Path:
    data = {
        "app_name": APP,
        "app_description": "CLI and MCP rendering test",
        "include_frontend": False,
        "include_mobile": False,
        "github_user": "testuser",
        "logto_endpoint": "https://logto.example.com",
        "deploy_target": "render",
        **extra,
    }
    template_mod.render_template(
        template_dir=TEMPLATE_DIR, output_dir=tmp_path, data=data, variant=variant
    )
    return tmp_path / APP


MCP_FILES = [
    "app/mcp_server/__init__.py",
    "app/mcp_server/server.py",
    "app/cli/commands/items.py",
    "tests/unit/test_cli_operations.py",
    "tests/unit/test_mcp_server.py",
]


@pytest.mark.parametrize("variant", ["full", "lightweight-api"])
def test_backend_variants_render_cli_and_mcp(tmp_path, variant):
    app_dir = render(tmp_path, variant=variant)
    for rel in MCP_FILES + ["app/cli/operations.py"]:
        assert (app_dir / rel).is_file(), rel
    pyproject = (app_dir / "pyproject.toml").read_text()
    assert f'{APP}-mcp = "app.mcp_server:main"' in pyproject
    assert 'mcp = ["mcp>=2.3,<3"]' in pyproject
    routes = (app_dir / "app/routes/items.py").read_text()
    for op_id in ("items_list", "items_get", "items_create", "items_update", "items_delete"):
        assert f'operation_id="{op_id}"' in routes


def test_static_dashboard_with_auth_keeps_cli_without_items_or_mcp(tmp_path):
    app_dir = render(tmp_path, variant="static-dashboard", with_auth=True)
    assert (app_dir / "app/cli/main.py").is_file()
    assert (app_dir / "app/cli/operations.py").is_file()
    for rel in MCP_FILES:
        assert not (app_dir / rel).exists(), rel
    pyproject = (app_dir / "pyproject.toml").read_text()
    assert "-mcp =" not in pyproject
    assert "mcp>=" not in pyproject
    main = (app_dir / "app/cli/main.py").read_text()
    assert "items_cmd" not in main
    for doc in ("README.md", "AGENT_CONTEXT.md"):
        text = (app_dir / doc).read_text()
        assert f"{APP}-mcp" not in text, doc
        assert f"{APP} items" not in text, doc


def test_default_render_uses_logto_machine_auth(tmp_path):
    client = (render(tmp_path) / "app/cli/client.py").read_text()
    assert 'AUTH_PROVIDER = "logto"' in client


def test_google_answer_renders_agent_key_client(tmp_path):
    # `auth_provider` comes from the Google auth branch; Copier passes the
    # extra answer through, so the client side can be checked here too.
    client = (render(tmp_path, auth_provider="google") / "app/cli/client.py").read_text()
    assert 'AUTH_PROVIDER = "google"' in client
    assert f"{APP_UPPER}_AGENT_KEY_FILE" in client


def test_client_has_no_systemexit(tmp_path):
    # The MCP server is long-running; a SystemExit in the client would kill it.
    client = (render(tmp_path) / "app/cli/client.py").read_text()
    assert "raise SystemExit" not in client


def test_mcp_docs_never_inline_secrets(tmp_path):
    app_dir = render(tmp_path)
    readme = (app_dir / "README.md").read_text()
    # Credentials in the host config example are ${VAR} references only.
    assert f'"{APP_UPPER}_M2M_APP_SECRET": "${{{APP_UPPER}_M2M_APP_SECRET}}"' in readme
    assert "--scope local" in readme
    for doc in ("README.md", "AGENT_CONTEXT.md"):
        text = (app_dir / doc).read_text()
        assert f"{APP}-mcp" in text, doc
        assert f"{APP_UPPER}_MCP_ALLOW_WRITES" in text, doc


def test_google_docs_describe_agent_key_not_logto(tmp_path):
    app_dir = render(tmp_path, auth_provider="google")
    readme = (app_dir / "README.md").read_text()
    context = (app_dir / "AGENT_CONTEXT.md").read_text()
    assert f'"{APP_UPPER}_AGENT_KEY_FILE": "${{{APP_UPPER}_AGENT_KEY_FILE}}"' in readme
    assert "python -m app.auth.agent_keys new" in readme
    assert f"{APP_UPPER}_M2M_APP_SECRET" not in readme.split("## CLI and MCP Server")[1].split(
        "## Per-App"
    )[0]
    assert "agent key" in context
    assert f"{APP_UPPER}_M2M_APP_SECRET" not in context


@pytest.fixture
def imported_mcp(tmp_path, monkeypatch):
    app_dir = render(tmp_path)
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    monkeypatch.syspath_prepend(str(app_dir))
    monkeypatch.delenv(f"{APP_UPPER}_MCP_ALLOW_WRITES", raising=False)
    yield importlib.import_module("app.mcp_server.server")
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


async def test_rendered_mcp_server_is_read_only_by_default(imported_mcp):
    from mcp import Client

    async with Client(imported_mcp.build_server()) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"items_list", "items_get"}
