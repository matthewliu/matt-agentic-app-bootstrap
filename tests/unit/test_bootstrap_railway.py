"""Tests for ``agentic_core.bootstrap.railway`` — railway CLI wrapper.

Same stub strategy as :mod:`tests.unit.test_bootstrap_github`: patch
``subprocess.run`` at the import site and assert on argv + structured
return dict.

The ``--railway-workspace`` thread-through is the critical contract for
this task — agent invocations always pass it, manual invocations omit it,
and the wrapper must mirror that exactly.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from agentic_core.bootstrap import railway


# A representative (assumed) ``railway init --json`` payload. The exact
# per-command schema is undocumented (KTD2 / plan Risk R-a) — these fixtures
# encode the shape the tolerant parser targets and MUST be validated against
# real ``railway ... --json`` output during live bootstrap.
_INIT_JSON = json.dumps(
    {
        "project": {
            "id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
            "name": "foo",
            "services": [{"id": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "name": "foo"}],
        }
    }
)


# ---------------------------------------------------------------------------
# create_railway_project — workspace flag thread-through + --json parsing
# ---------------------------------------------------------------------------


class TestCreateRailwayProject:
    def test_with_workspace_invokes_railway_init_with_w_flag(self):
        """Agent invocation: workspace='agent-sandbox' → argv has `-w agent-sandbox` + `--json`."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_INIT_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.create_railway_project("foo", workspace="agent-sandbox")

        argv = mock_run.call_args.args[0]
        # Exact argv shape the task spec calls out — now with the global --json flag.
        assert argv == ["railway", "init", "-n", "foo", "--json", "-w", "agent-sandbox"]
        assert result["status"] == "success"

    def test_init_argv_includes_json(self):
        """R3: init must carry the global --json flag so we parse structured output."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_INIT_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.create_railway_project("foo")

        argv = mock_run.call_args.args[0]
        assert "--json" in argv

    def test_init_parses_and_returns_project_id(self):
        """R2: the parsed Railway project id is surfaced for persistence to state."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_INIT_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.create_railway_project("foo")

        assert result["status"] == "success"
        assert result["project_id"] == "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

    def test_init_malformed_json_returns_cli_error_not_crash(self):
        """Defensive parse: returncode 0 but unparseable stdout → structured failure."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="ServiceDomain created 🚀 not-json", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.create_railway_project("foo")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_CLI_ERROR"

    def test_init_empty_stdout_returns_cli_error(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.create_railway_project("foo")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_CLI_ERROR"

    def test_without_workspace_omits_w_flag(self):
        """the manual path: workspace=None → argv has NO `-w`."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_INIT_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.create_railway_project("foo", workspace=None)

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "init", "-n", "foo", "--json"]
        assert "-w" not in argv
        assert result["status"] == "success"

    def test_default_workspace_none_omits_w_flag(self):
        """workspace defaults to None."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_INIT_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.create_railway_project("foo")

        argv = mock_run.call_args.args[0]
        assert "-w" not in argv

    def test_empty_string_workspace_treated_as_none(self):
        """Defensive: ``--railway-workspace ""`` from a shell shouldn't add `-w ""`."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_INIT_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.create_railway_project("foo", workspace="")

        argv = mock_run.call_args.args[0]
        assert "-w" not in argv

    def test_failure_returns_structured_dict(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: unauthorized — run `railway login`\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.create_railway_project("foo", workspace="agent-sandbox")

        assert result["status"] == "failed"
        assert result["error_code"] in {"RAILWAY_CLI_ERROR", "RAILWAY_AUTH_FAILED"}
        assert result["error_code"] == "RAILWAY_AUTH_FAILED"
        assert result["exit_code"] == 1

    def test_timeout_returns_structured_failure_not_raise(self):
        with patch.object(
            railway.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["railway"], timeout=60),
        ):
            result = railway.create_railway_project("foo")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_TIMEOUT"
        assert "timeout" in result["error_message"].lower()

    def test_default_timeout_is_60s(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.create_railway_project("foo")

        assert mock_run.call_args.kwargs.get("timeout") == 60

    def test_timeout_override(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.create_railway_project("foo", timeout=120)

        assert mock_run.call_args.kwargs.get("timeout") == 120


# ---------------------------------------------------------------------------
# create_railway_service — creates a service in the linked project so
# subsequent `railway variables --set` / `railway up` have a target. A freshly
# `railway init`'d project has NO service, so this MUST run before env/deploy
# (the live "Project has no services" failure this fixes).
# ---------------------------------------------------------------------------


# A representative (assumed) `railway add --service <name> --json` payload.
_ADD_SERVICE_JSON = json.dumps(
    {"project": {"id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8"},
     "service": {"id": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "name": "foo"}}
)


class TestCreateRailwayService:
    def test_argv_is_add_service_json(self):
        """argv is exactly `railway add --service <name> --json`."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_ADD_SERVICE_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.create_railway_service("foo")

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "add", "--service", "foo", "--json"]
        assert result["status"] == "success"
        assert result["service"] == "foo"

    def test_success_returns_service_id_when_present(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_ADD_SERVICE_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.create_railway_service("foo")

        assert result["status"] == "success"
        assert result["service_id"] == "f47ac10b-58cc-4372-a567-0e02b2c3d479"

    def test_success_service_id_none_when_unparseable(self):
        """A zero exit with an empty/undocumented body is still a success — the
        service was created; we just couldn't extract its id (id is best-effort,
        unlike create_railway_project's project_id which gates success)."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.create_railway_service("foo")

        assert result["status"] == "success"
        assert result["service_id"] is None

    def test_already_exists_is_adopted_success(self):
        """Idempotency (mirrors delete-project already-absent): a service that
        already exists on re-run/resume is a tolerated success, flagged adopted —
        NOT a failure."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: A service named 'foo' already exists\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.create_railway_service("foo")

        assert result["status"] == "success"
        assert result["adopted"] is True

    def test_auth_failure_is_not_adopted(self):
        """A genuine auth error must still surface as a failure (not adopted)."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: unauthorized — run `railway login`\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.create_railway_service("foo")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_AUTH_FAILED"
        assert result.get("adopted") is not True

    def test_forwards_cwd(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_ADD_SERVICE_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.create_railway_service("foo", cwd="/app/dir")
        assert mock_run.call_args.kwargs.get("cwd") == "/app/dir"

    def test_timeout_returns_structured_failure(self):
        with patch.object(
            railway.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["railway"], timeout=60),
        ):
            result = railway.create_railway_service("foo")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_TIMEOUT"
        assert "timeout" in result["error_message"].lower()


# ---------------------------------------------------------------------------
# set_env_var
# ---------------------------------------------------------------------------


class TestSetEnvVar:
    def test_invokes_railway_variables_set(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.set_env_var("LOGTO_APP_ID", "abc123")

        argv = mock_run.call_args.args[0]
        assert argv[:3] == ["railway", "variables", "--set"]
        # `--set NAME=VALUE` is the documented form; the wrapper must
        # serialize that way (one --set per call).
        assert "LOGTO_APP_ID=abc123" in argv
        assert result["status"] == "success"

    def test_always_appends_skip_deploys(self):
        """We deploy once at the end (STEP_RAILWAY_DEPLOY), so every var-set must
        pass --skip-deploys to avoid triggering a deploy per var."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.set_env_var("FOO", "bar")

        argv = mock_run.call_args.args[0]
        assert "--skip-deploys" in argv

    def test_service_appends_dash_s_flag(self):
        """When a service is named, argv targets it with `-s <service>` (required
        when the project has multiple services)."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.set_env_var("FOO", "bar", service="foo")

        argv = mock_run.call_args.args[0]
        assert "-s" in argv
        assert argv[argv.index("-s") + 1] == "foo"
        assert "--skip-deploys" in argv

    def test_no_service_omits_dash_s(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.set_env_var("FOO", "bar")

        argv = mock_run.call_args.args[0]
        assert "-s" not in argv

    def test_returned_dict_omits_secret_value(self):
        """Returned struct must not echo the env var value (it could be a secret)."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.set_env_var("LOGTO_APP_SECRET", "supersecret-2244")

        serialized = json.dumps(result)
        assert "supersecret-2244" not in serialized

    def test_failure_returns_structured_dict(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: no project linked\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.set_env_var("FOO", "bar")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_CLI_ERROR"

    def test_timeout_returns_structured_failure(self):
        with patch.object(
            railway.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["railway"], timeout=60),
        ):
            result = railway.set_env_var("FOO", "bar")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_TIMEOUT"


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


class TestDeploy:
    def test_detached_default_invokes_railway_up_dash_d(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Deployment started\n",
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.deploy()

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "up", "-d"]
        assert result["status"] == "success"

    def test_detached_false_omits_dash_d(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.deploy(detached=False)

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "up"]
        assert "-d" not in argv

    def test_service_appends_dash_s_and_forwards_cwd(self):
        """`railway up` deploys the CURRENT DIRECTORY's code, so it must run with
        cwd = the rendered app dir; `-s <service>` targets the right service."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.deploy(service="foo", cwd="/x")

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "up", "-d", "-s", "foo"]
        assert mock_run.call_args.kwargs.get("cwd") == "/x"

    def test_link_project_argv_and_forwards_cwd(self):
        """Live-validated: link the app dir to project+service+env before deploy
        (`railway link -p <id> -s <service> -e production`)."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.link_project(
                project_id="proj-123", service="my-sample-notes-app", cwd="/apps/x"
            )
        argv = mock_run.call_args.args[0]
        assert argv[:2] == ["railway", "link"]
        assert argv[argv.index("-p") + 1] == "proj-123"
        assert argv[argv.index("-s") + 1] == "my-sample-notes-app"
        assert argv[argv.index("-e") + 1] == "production"
        assert mock_run.call_args.kwargs.get("cwd") == "/apps/x"
        assert result["status"] == "success"

    def test_deploy_is_bare_up_relying_on_link(self):
        """Deploy is a plain `railway up -d [-s]` — it relies on the caller
        having linked the cwd (no path/-p/-e, which tripped 'prefix not found')."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.deploy(service="my-sample-notes-app", cwd="/apps/x")
        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "up", "-d", "-s", "my-sample-notes-app"]
        assert mock_run.call_args.kwargs.get("cwd") == "/apps/x"
        assert "-p" not in argv and "-e" not in argv

    def test_no_service_omits_dash_s(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.deploy()

        argv = mock_run.call_args.args[0]
        assert "-s" not in argv

    def test_deploy_failure_returns_structured_dict(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Deployment failed\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.deploy()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_CLI_ERROR"
        assert result["exit_code"] == 1

    def test_deploy_timeout_returns_structured_failure(self):
        with patch.object(
            railway.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["railway"], timeout=60),
        ):
            result = railway.deploy()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_TIMEOUT"


# ---------------------------------------------------------------------------
# get_linked_project — the check() helper that lets a resume ADOPT an
# already-linked Railway project instead of re-running `railway init`
# (which is NOT idempotent and fails on re-run). R3 / SC2.
# ---------------------------------------------------------------------------


class TestGetLinkedProject:
    def test_argv_is_status_json(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"project": {"id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "name": "foo"}}
            ),
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.get_linked_project()

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "status", "--json"]

    def test_linked_project_returns_id(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"project": {"id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "name": "foo"}}
            ),
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_linked_project()

        assert result["linked"] is True
        assert result["project_id"] == "6ba7b810-9dad-11d1-80b4-00c04fd430c8"

    def test_no_linked_project_when_status_errors(self):
        """`railway status` exits non-zero when nothing is linked → linked False."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="No linked project found. Run `railway link`\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_linked_project()

        assert result["linked"] is False
        assert result.get("project_id") is None

    def test_malformed_json_is_treated_as_not_linked(self):
        """A check() helper must never crash — unparseable output → not linked
        so the caller proceeds to `init` rather than blowing up."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json at all", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_linked_project()

        assert result["linked"] is False

    def test_timeout_is_treated_as_not_linked(self):
        with patch.object(
            railway.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["railway"], timeout=60),
        ):
            result = railway.get_linked_project()

        assert result["linked"] is False

    def test_linked_project_returns_name_nested(self):
        """Finding 1: the linked project's NAME must be extracted so the caller
        can verify it matches the app slug before adopting it."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"project": {"id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "name": "myapp"}}
            ),
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_linked_project()

        assert result["linked"] is True
        assert result["name"] == "myapp"

    def test_linked_project_returns_name_flat(self):
        """Tolerate a flat top-level ``name`` key too (schema undocumented)."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "name": "flatapp"}
            ),
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_linked_project()

        assert result["linked"] is True
        assert result["name"] == "flatapp"

    def test_linked_project_name_none_when_unextractable(self):
        """When the JSON has a project id but no name-shaped key, ``name`` is
        None so the caller can refuse to adopt an unverifiable project."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"project": {"id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8"}}
            ),
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_linked_project()

        assert result["linked"] is True
        assert result["name"] is None


# ---------------------------------------------------------------------------
# add_database — provisions Postgres via `railway add --database postgres --json`
# (U5 / KTD4). Non-interactive, auto-links + auto-deploys, exposes DATABASE_URL.
# ---------------------------------------------------------------------------


# A representative (assumed) `railway add --database postgres --json` payload.
# The exact schema is undocumented (KTD4 / plan Risk R-a) — the parser only
# needs *some* valid JSON to treat the add as a success; malformed/empty
# stdout on a zero exit is the corrupted-output failure class we reject.
_ADD_DB_JSON = json.dumps(
    {"project": {"id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8"},
     "service": {"id": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "name": "Postgres"}}
)


class TestAddDatabase:
    def test_argv_is_add_database_postgres_json(self):
        """R4/KTD4: argv is exactly `railway add --database postgres --json`."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_ADD_DB_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.add_database()

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "add", "--database", "postgres", "--json"]
        assert result["status"] == "success"

    def test_kind_is_threaded_into_database_flag(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_ADD_DB_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.add_database(kind="postgres")

        argv = mock_run.call_args.args[0]
        assert argv[:4] == ["railway", "add", "--database", "postgres"]
        assert "--json" in argv

    def test_success_dict_never_echoes_a_connection_string(self):
        """DATABASE_URL is secret-shaped — the return dict must not carry it even
        if the CLI echoes one in --json output."""
        payload = json.dumps(
            {"variables": {"DATABASE_URL": "postgres://user:pw@host:5432/db"}}
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.add_database()

        assert result["status"] == "success"
        assert "postgres://user:pw@host:5432/db" not in json.dumps(result)

    def test_malformed_json_returns_cli_error_not_crash(self):
        """Zero exit but unparseable stdout → structured RAILWAY_CLI_ERROR."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Postgres added 🚀 not-json", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.add_database()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_CLI_ERROR"

    def test_empty_stdout_returns_cli_error(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.add_database()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_CLI_ERROR"

    def test_nonzero_exit_returns_structured_failure(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: unauthorized — run `railway login`\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.add_database()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_AUTH_FAILED"
        assert result["exit_code"] == 1

    def test_timeout_returns_structured_failure_not_raise(self):
        with patch.object(
            railway.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["railway"], timeout=60),
        ):
            result = railway.add_database()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_TIMEOUT"
        assert "timeout" in result["error_message"].lower()


# ---------------------------------------------------------------------------
# has_database — the check() helper letting a resume ADOPT an existing DB
# instead of provisioning a second one (U5 / KTD4 / Risk R-c). Non-fatal:
# any ambiguity degrades to has_database=False so the caller provisions.
# ---------------------------------------------------------------------------


class TestHasDatabase:
    def test_argv_is_variables_json(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"FOO": "bar"}), stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.has_database()

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "variables", "--json"]

    def test_database_url_present_flat_returns_true(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"DATABASE_URL": "postgres://x", "OTHER": "y"}),
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.has_database()

        assert result["has_database"] is True

    def test_database_url_present_nested_returns_true(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"Postgres": {"DATABASE_URL": "postgres://x"}}),
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.has_database()

        assert result["has_database"] is True

    def test_no_database_url_returns_false(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"FOO": "bar"}), stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.has_database()

        assert result["has_database"] is False

    def test_does_not_echo_connection_string(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({"DATABASE_URL": "postgres://user:pw@host/db"}),
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.has_database()

        assert "postgres://user:pw@host/db" not in json.dumps(result)

    def test_nonzero_exit_is_not_has_database(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no project linked\n"
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.has_database()

        assert result["has_database"] is False

    def test_malformed_json_is_not_has_database(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.has_database()

        assert result["has_database"] is False

    def test_timeout_is_not_has_database(self):
        with patch.object(
            railway.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["railway"], timeout=60),
        ):
            result = railway.has_database()

        assert result["has_database"] is False


# ---------------------------------------------------------------------------
# get_prod_domain — captures the assigned prod domain via `railway domain
# --json` (U6). An early live-run incident came from parsing DECORATED
# `railway domain` stdout (``ServiceDomain created 🚀 https://…``); this helper
# uses --json and the tolerant _loads/_extract_* parse layer instead.
# ---------------------------------------------------------------------------


# A representative (assumed) `railway domain --json` payload. Exact schema is
# undocumented (Risk R-a) — the tolerant parser targets this shape and MUST be
# validated against real output during live bootstrap.
_DOMAIN_JSON = json.dumps({"domain": "foo-production.up.railway.app"})


class TestGetProdDomain:
    def test_argv_is_domain_json(self):
        """R3/R4: argv is exactly `railway domain --json` — no decorated stdout."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_DOMAIN_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.get_prod_domain()

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "domain", "--json"]
        assert "--json" in argv
        assert result["status"] == "success"

    def test_service_appends_dash_s_flag(self):
        """A named service targets `railway domain -s <service>` (required when
        the project has multiple services)."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_DOMAIN_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.get_prod_domain(service="foo")

        argv = mock_run.call_args.args[0]
        assert "-s" in argv
        assert argv[argv.index("-s") + 1] == "foo"

    def test_no_service_omits_dash_s(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_DOMAIN_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.get_prod_domain()

        argv = mock_run.call_args.args[0]
        assert "-s" not in argv

    def test_parses_flat_domain(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_DOMAIN_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_prod_domain()

        assert result["status"] == "success"
        assert result["domain"] == "foo-production.up.railway.app"

    def test_parses_domains_list_of_dicts(self):
        payload = json.dumps(
            {"domains": [{"domain": "foo-production.up.railway.app"}]}
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_prod_domain()

        assert result["domain"] == "foo-production.up.railway.app"

    def test_parses_service_domains_list_of_strings(self):
        payload = json.dumps({"serviceDomains": ["foo-production.up.railway.app"]})
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_prod_domain()

        assert result["domain"] == "foo-production.up.railway.app"

    def test_strips_scheme_and_path_to_bare_host(self):
        """A URL-shaped value degrades to a bare hostname the caller can wrap
        as https://<host>/callback."""
        payload = json.dumps({"domain": "https://foo-production.up.railway.app/"})
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=payload, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_prod_domain()

        assert result["domain"] == "foo-production.up.railway.app"

    def test_decorated_stdout_returns_cli_error_not_a_domain(self):
        """The original bug: decorated stdout must NOT be mistaken for a domain."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="ServiceDomain created 🚀 https://foo-production.up.railway.app",
            stderr="",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_prod_domain()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_CLI_ERROR"

    def test_empty_stdout_returns_cli_error(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_prod_domain()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_CLI_ERROR"

    def test_nonzero_exit_returns_structured_failure(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: unauthorized — run `railway login`\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.get_prod_domain()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_AUTH_FAILED"
        assert result["exit_code"] == 1

    def test_timeout_returns_structured_failure_not_raise(self):
        with patch.object(
            railway.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["railway"], timeout=60),
        ):
            result = railway.get_prod_domain()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_TIMEOUT"
        assert "timeout" in result["error_message"].lower()


# ---------------------------------------------------------------------------
# delete_project — non-interactive Railway project delete for teardown (U8).
# `railway delete --project <id> --yes --json`. `--yes` makes the CLI
# non-interactive; the operator-facing confirmation lives in the teardown
# layer, NOT here.
# ---------------------------------------------------------------------------


class TestDeleteProject:
    def test_argv_is_delete_project_yes_json(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"status": "deleting"}), stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.delete_project(project_id="proj-abc")

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "delete", "--project", "proj-abc", "--yes", "--json"]
        assert result["status"] == "success"

    def test_accepts_name_when_no_project_id(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            result = railway.delete_project(name="foo")

        argv = mock_run.call_args.args[0]
        assert argv == ["railway", "delete", "--project", "foo", "--yes", "--json"]
        assert result["status"] == "success"

    def test_zero_exit_is_success_even_without_json_body(self):
        """Railway docs: treat a successful response as the deletion request
        being accepted — no --json body required (deletion is async)."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.delete_project(project_id="proj-abc")

        assert result["status"] == "success"

    def test_missing_target_returns_failure_without_subprocess(self):
        with patch.object(railway.subprocess, "run") as mock_run:
            result = railway.delete_project()

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_CLI_ERROR"
        mock_run.assert_not_called()

    def test_nonzero_exit_returns_structured_failure(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: unauthorized — run `railway login`\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.delete_project(project_id="proj-abc")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_AUTH_FAILED"
        assert result["exit_code"] == 1

    def test_timeout_returns_structured_failure_not_raise(self):
        with patch.object(
            railway.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["railway"], timeout=60),
        ):
            result = railway.delete_project(project_id="proj-abc")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_TIMEOUT"
        assert "timeout" in result["error_message"].lower()

    def test_already_absent_stderr_is_success_flagged(self):
        """Fix #17: a 'project not found' stderr on non-zero exit is tolerated
        as already-absent (re-run teardown after the project is gone), not a
        failure — mirroring Logto deletes' allow_missing."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: Project not found\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.delete_project(project_id="proj-gone")

        assert result["status"] == "success"
        assert result["already_absent"] is True

    def test_already_absent_does_not_exist_variant(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: project does not exist\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.delete_project(project_id="proj-gone")

        assert result["status"] == "success"
        assert result["already_absent"] is True

    def test_auth_error_is_not_treated_as_already_absent(self):
        """Conservative: genuine auth/other errors still fail (not already-absent)."""
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="Error: unauthorized — run `railway login`\n",
        )
        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.delete_project(project_id="proj-abc")

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_AUTH_FAILED"
        assert result.get("already_absent") is not True


# ---------------------------------------------------------------------------
# Fix #8: every railway subprocess call must target the app dir via cwd=,
# not the orchestrator's process cwd (railway up deploys cwd contents;
# railway init links cwd). Each public function forwards an optional cwd
# through to subprocess.run(..., cwd=cwd). Default None = current behavior.
# ---------------------------------------------------------------------------


class TestCwdForwarding:
    def test_create_railway_project_forwards_cwd(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_INIT_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.create_railway_project("foo", cwd="/app/dir")
        assert mock_run.call_args.kwargs.get("cwd") == "/app/dir"

    def test_set_env_var_forwards_cwd(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.set_env_var("FOO", "bar", cwd="/app/dir")
        assert mock_run.call_args.kwargs.get("cwd") == "/app/dir"

    def test_deploy_forwards_cwd(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.deploy(cwd="/app/dir")
        assert mock_run.call_args.kwargs.get("cwd") == "/app/dir"

    def test_get_linked_project_forwards_cwd(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps({"project": {"id": "x", "name": "foo"}}), stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.get_linked_project(cwd="/app/dir")
        assert mock_run.call_args.kwargs.get("cwd") == "/app/dir"

    def test_has_database_forwards_cwd(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps({"FOO": "bar"}), stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.has_database(cwd="/app/dir")
        assert mock_run.call_args.kwargs.get("cwd") == "/app/dir"

    def test_add_database_forwards_cwd(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_ADD_DB_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.add_database(cwd="/app/dir")
        assert mock_run.call_args.kwargs.get("cwd") == "/app/dir"

    def test_get_prod_domain_forwards_cwd(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_DOMAIN_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.get_prod_domain(cwd="/app/dir")
        assert mock_run.call_args.kwargs.get("cwd") == "/app/dir"

    def test_delete_project_forwards_cwd(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.delete_project(project_id="proj-abc", cwd="/app/dir")
        assert mock_run.call_args.kwargs.get("cwd") == "/app/dir"

    def test_default_cwd_is_none(self):
        """Backward compatible: callers that omit cwd get cwd=None (unchanged)."""
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=_INIT_JSON, stderr=""
        )
        with patch.object(railway.subprocess, "run", return_value=completed) as mock_run:
            railway.create_railway_project("foo")
        assert mock_run.call_args.kwargs.get("cwd") is None


class TestWaitForDeployment:
    def test_returns_failure_as_soon_as_latest_deployment_fails(self):
        building = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps([{"id": "dep-1", "status": "BUILDING"}]),
            stderr="",
        )
        failed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps([{"id": "dep-1", "status": "FAILED"}]),
            stderr="",
        )
        statuses: list[str] = []

        with patch.object(
            railway.subprocess, "run", side_effect=[building, failed]
        ) as mock_run:
            result = railway.wait_for_deployment(
                service="foo",
                cwd="/apps/foo",
                poll_interval=0,
                timeout=30,
                on_status=statuses.append,
            )

        assert result["status"] == "failed"
        assert result["error_code"] == "RAILWAY_DEPLOY_FAILED"
        assert statuses == ["BUILDING", "FAILED"]
        assert all(call.kwargs.get("cwd") == "/apps/foo" for call in mock_run.call_args_list)

    def test_returns_success_when_latest_deployment_succeeds(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout=json.dumps([{"id": "dep-1", "status": "SUCCESS"}]),
            stderr="",
        )

        with patch.object(railway.subprocess, "run", return_value=completed):
            result = railway.wait_for_deployment(
                service="foo", poll_interval=0, timeout=30
            )

        assert result == {
            "status": "success",
            "deployment_id": "dep-1",
            "deployment_status": "SUCCESS",
        }
