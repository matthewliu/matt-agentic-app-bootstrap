"""End-to-end test: a generated Google-auth app through its CLI and MCP server.

Renders the template with ``auth_provider=google``, installs the app in its
own venv, and starts the real API with uvicorn on localhost (SQLite file DB,
dummy Google settings). Agent keys come from the app's own operator tool,
``python -m app.auth.agent_keys new``. The test then drives the installed
``<slug>`` CLI and the ``<slug>-mcp`` stdio server against that API.

Nothing leaves the machine: no Google call (no browser sign-in happens), no
cloud resource, no real secret. ``HOME`` points at a temp dir so the app never
reads the operator's ``~/.agentic-apps.env``.

The machine-auth contract under test is
``docs/agent-auth-contract.md``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx
import pytest

from agentic_core.bootstrap import template as template_mod

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"
APP = "gauth-app"
APP_UPPER = "GAUTH_APP"
OWNER = "google:1234"
OTHER_USER = "google:5678"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_env(tmp_path_factory):
    """Render, install, create keys, and start the API. Yields shared state."""
    root = tmp_path_factory.mktemp("google-e2e")
    template_mod.render_template(
        template_dir=TEMPLATE_DIR,
        output_dir=root,
        data={
            "app_name": APP,
            "app_description": "Google CLI and MCP end-to-end",
            "include_frontend": False,
            "include_mobile": False,
            "github_user": "testuser",
            "deploy_target": "render",
            "auth_provider": "google",
        },
        variant="full",
    )
    app_dir = root / APP
    assert (app_dir / "app/mcp_server/server.py").is_file()

    venv = app_dir / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=60)
    pip = subprocess.run(
        [str(venv / "bin/pip"), "install", "-q", "-e", ".[dev]"],
        cwd=app_dir, capture_output=True, text=True, timeout=600,
    )
    assert pip.returncode == 0, pip.stderr[-2000:]

    home = root / "home"
    home.mkdir()
    keys_dir = root / "keys"
    keys_dir.mkdir()
    base_env = {"PATH": os.environ["PATH"], "HOME": str(home)}

    def new_key(key_id: str, actor: str, write: bool) -> tuple[Path, dict]:
        out = keys_dir / f"{key_id}.agent-key"
        argv = [
            str(venv / "bin/python"), "-m", "app.auth.agent_keys", "new",
            "--id", key_id, "--actor", actor, "--out", str(out),
        ] + (["--write"] if write else [])
        proc = subprocess.run(
            argv, cwd=app_dir, env=base_env, capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(proc.stdout)
        # The operator tool must never print the key itself.
        assert out.read_text().strip() not in proc.stdout
        return out, result["entry"]

    keys = {
        "rw": new_key("owner-rw", OWNER, write=True),
        "ro": new_key("owner-ro", OWNER, write=False),
        "other": new_key("other-rw", OTHER_USER, write=True),
    }

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_env = {
        **base_env,
        "DATABASE_URL": f"sqlite+aiosqlite:///{root / 'e2e.db'}",
        "AUTO_CREATE_TABLES": "1",
        "GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-secret",
        "SESSION_SECRET": "s" * 48,
        "AUTH_ALLOWED_EMAILS": "owner@example.com",
        "APP_BASE_URL": base_url,
        "AGENT_CREDENTIALS": json.dumps([entry for _, entry in keys.values()]),
    }
    server = subprocess.Popen(
        [str(venv / "bin/uvicorn"), "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=app_dir, env=server_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if server.poll() is not None or time.monotonic() > deadline:
                server.kill()
                pytest.fail(f"API did not start:\n{server.stdout.read()[-3000:]}")
            time.sleep(0.2)

        yield {
            "app_dir": app_dir,
            "venv": venv,
            "base_env": base_env,
            "base_url": base_url,
            "keys": keys,
        }
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def client_env(state, key: str, actor: str = OWNER, **extra) -> dict[str, str]:
    key_file, _ = state["keys"][key]
    return {
        **state["base_env"],
        f"{APP_UPPER}_API_URL": state["base_url"],
        f"{APP_UPPER}_USER_SUB": actor,
        f"{APP_UPPER}_AGENT_KEY_FILE": str(key_file),
        **extra,
    }


def cli(state, env, *args) -> tuple[int, dict]:
    proc = subprocess.run(
        [str(state["venv"] / "bin" / APP), "--json", *args],
        cwd=state["app_dir"], env=env, capture_output=True, text=True, timeout=60,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout must be one JSON line: {proc.stdout!r} {proc.stderr!r}"
    return proc.returncode, json.loads(lines[0])


async def mcp_call(state, env, tool: str, args: dict):
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(
        command=str(state["venv"] / "bin" / f"{APP}-mcp"), args=[], env=env
    )
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        return names, await c.call_tool(tool, args)


def test_owner_creates_with_cli_and_reads_with_mcp(app_env):
    code, item = cli(app_env, client_env(app_env, "rw"), "items", "create", "--name", "E2E one")
    assert code == 0, json.dumps(item)
    names, result = anyio.run(
        mcp_call, app_env, client_env(app_env, "rw"), "items_list", {}
    )
    assert names == {"items_list", "items_get"}
    assert not result.is_error, result.content
    assert item["id"] in {i["id"] for i in result.structured_content["items"]}


def test_mcp_write_with_read_write_key_and_opt_in(app_env):
    env = client_env(app_env, "rw", **{f"{APP_UPPER}_MCP_ALLOW_WRITES": "true"})
    names, created = anyio.run(mcp_call, app_env, env, "items_create", {"name": "via mcp"})
    assert {"items_create", "items_update"} <= names
    assert not created.is_error, created.content
    item_id = created.structured_content["id"]
    code, fetched = cli(app_env, client_env(app_env, "rw"), "items", "get", item_id)
    assert code == 0, json.dumps(fetched)
    assert fetched["name"] == "via mcp"


def test_read_only_key_cannot_write(app_env):
    code, out = cli(app_env, client_env(app_env, "ro"), "items", "create", "--name", "nope")
    assert code == 3
    assert out["error_code"] == "missing_scope"
    code, out = cli(app_env, client_env(app_env, "ro"), "items", "list")
    assert code == 0, json.dumps(out)


def test_server_enforces_scope_even_when_mcp_writes_are_on(app_env):
    env = client_env(app_env, "ro", **{f"{APP_UPPER}_MCP_ALLOW_WRITES": "true"})
    names, result = anyio.run(
        mcp_call, app_env, env, "items_create", {"name": "via mcp"}
    )
    assert "items_create" in names
    assert result.is_error
    assert "missing_scope" in result.content[0].text


def test_key_bound_to_another_actor_is_refused(app_env):
    code, out = cli(app_env, client_env(app_env, "other", actor=OWNER), "items", "list")
    assert code == 3
    assert out["error_code"] == "actor_mismatch"


def test_users_see_only_their_own_items(app_env):
    code, mine = cli(app_env, client_env(app_env, "rw"), "items", "create", "--name", "owner only")
    assert code == 0, json.dumps(mine)
    code, theirs = cli(app_env, client_env(app_env, "other", actor=OTHER_USER), "items", "list")
    assert code == 0, json.dumps(theirs)
    assert mine["id"] not in {i["id"] for i in theirs["items"]}


def test_wrong_secret_is_rejected(app_env, tmp_path):
    key_file, _ = app_env["keys"]["rw"]
    forged = tmp_path / "forged.agent-key"
    key_id = key_file.read_text().strip().split(".")[1]
    forged.write_text(f"agk.{key_id}." + "Z" * 43)
    forged.chmod(0o600)
    env = client_env(app_env, "rw", **{f"{APP_UPPER}_AGENT_KEY_FILE": str(forged)})
    code, out = cli(app_env, env, "items", "list")
    assert code == 2
    assert out["error_code"] == "invalid_agent_credential"


def test_google_token_is_not_a_machine_credential(app_env):
    jwt_like = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"
    env = client_env(app_env, "rw")
    del env[f"{APP_UPPER}_AGENT_KEY_FILE"]
    env[f"{APP_UPPER}_AGENT_KEY"] = jwt_like
    code, out = cli(app_env, env, "items", "list")
    assert code == 5
    assert out["error_code"] == "misconfigured"
    assert jwt_like not in out["error_message"]


def test_bearer_jwt_sent_directly_is_rejected_by_the_server(app_env):
    resp = httpx.get(
        f"{app_env['base_url']}/api/items",
        headers={
            "Authorization": "Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2ln",
            f"X-{APP_UPPER}-Actor-Sub": OWNER,
        },
        timeout=5,
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_agent_credential"
