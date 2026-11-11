"""Tests for ``agentic_core.bootstrap.render`` — Render CLI wrapper (U4 / KTD2).

Same stub strategy as :mod:`tests.unit.test_bootstrap_railway`: patch
``subprocess.run`` at the import site and assert on argv + the structured
return dict.

The fixtures below are the REAL output shapes of Render CLI v2.31.0,
observed with ``-o json`` on 2026-11-05 (stdout, stderr, and exit code).
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from agentic_core.bootstrap import render

# --- Observed: `render whoami -o json` (exit 0) -----------------------------
_WHOAMI_JSON = json.dumps(
    {
        "email": "owner@example.com",
        "id": "usr-d7lv08u7r5hc73ajj6ng",
        "name": "Example Owner",
    },
    indent=2,
)

# --- Observed: `render workspace current -o json` (exit 0) ------------------
_WORKSPACE_JSON = json.dumps(
    {
        "email": "owner@example.com",
        "id": "tea-dbgpa4on74is73d2vpk0",
        "name": "Example Workspace",
        "type": "team",
    },
    indent=2,
)

# --- Observed: logged out (empty CLI config), exit 1, stdout empty ----------
_NOT_LOGGED_IN_STDERR = "Error: failed to create client: run `render login` to authenticate\n"
# --- Observed: bad API key, exit 1, stdout empty ----------------------------
_EXPIRED_STDERR = "Error: your token is expired; run `render login` to get a new one\n"

# --- Observed: `render blueprints validate <valid> -o json` (exit 0) --------
_VALID_JSON = json.dumps(
    {
        "plan": {
            "databases": ["u4smoke-db"],
            "services": ["u4smoke"],
            "totalActions": 2,
        },
        "valid": True,
    },
    indent=2,
)

# --- Observed: `render blueprints validate <invalid> -o json` (exit 1) ------
_INVALID_JSON = json.dumps(
    {
        "errors": [
            {
                "column": 5,
                "error": "not-a-plan not a valid plan for service type web",
                "line": 5,
                "path": "services[0].plan",
            }
        ],
        "valid": False,
    },
    indent=2,
)

# --- Observed non-JSON validate failures (exit 1, stdout empty) -------------
_FILE_NOT_FOUND_STDERR = "Error: file not found: /tmp/app/render.yaml\n"
_404_STDERR = (
    'Error: validation request failed with status 404: '
    '{"message":"not found: owner: tea-doesnotexist000"}\n'
)
_401_STDERR = 'Error: validation request failed with status 401: {"message":"Unauthorized"}\n'
# 5xx shape is inferred from the observed 401/404 shape (same CLI code path).
_503_STDERR = (
    'Error: validation request failed with status 503: {"message":"Service Unavailable"}\n'
)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# whoami
# ---------------------------------------------------------------------------


class TestWhoami:
    def test_success_returns_email(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(0, _WHOAMI_JSON)
        ) as mock_run:
            result = render.whoami()

        assert mock_run.call_args.args[0] == ["render", "whoami", "-o", "json"]
        assert mock_run.call_args.kwargs["timeout"] == render.DEFAULT_TIMEOUT
        assert result == {
            "status": "success",
            "email": "owner@example.com",
            "user_id": "usr-d7lv08u7r5hc73ajj6ng",
            "name": "Example Owner",
        }

    def test_not_logged_in_is_auth_failed(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(1, "", _NOT_LOGGED_IN_STDERR)
        ):
            result = render.whoami()

        assert result["status"] == "failed"
        assert result["error_code"] == "RENDER_AUTH_FAILED"
        assert result["retryable"] is False
        assert result["exit_code"] == 1
        assert "render login" in result["error_message"]

    def test_expired_token_is_auth_failed(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(1, "", _EXPIRED_STDERR)
        ):
            result = render.whoami()

        assert result["error_code"] == "RENDER_AUTH_FAILED"
        assert result["retryable"] is False

    def test_plain_not_logged_in_text_is_auth_failed(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(1, "", "Error: not logged in\n")
        ):
            result = render.whoami()

        assert result["error_code"] == "RENDER_AUTH_FAILED"

    def test_missing_binary_is_cli_missing(self):
        with patch.object(
            render.subprocess, "run", side_effect=FileNotFoundError("render")
        ):
            result = render.whoami()

        assert result["status"] == "failed"
        assert result["error_code"] == "RENDER_CLI_MISSING"
        assert result["retryable"] is False
        assert result["exit_code"] is None

    def test_timeout_is_retryable(self):
        with patch.object(
            render.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["render"], timeout=5),
        ):
            result = render.whoami(timeout=5)

        assert result["error_code"] == "RENDER_TIMEOUT"
        assert result["retryable"] is True

    def test_malformed_json_is_structured_failure(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(0, "Logged in as owner")
        ):
            result = render.whoami()

        assert result["status"] == "failed"
        assert result["error_code"] == "RENDER_CLI_ERROR"
        assert result["retryable"] is False


# ---------------------------------------------------------------------------
# workspace current
# ---------------------------------------------------------------------------


class TestWorkspaceCurrent:
    def test_success_returns_id_and_name(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(0, _WORKSPACE_JSON)
        ) as mock_run:
            result = render.workspace_current()

        assert mock_run.call_args.args[0] == [
            "render",
            "workspace",
            "current",
            "-o",
            "json",
        ]
        assert result == {
            "status": "success",
            "workspace_id": "tea-dbgpa4on74is73d2vpk0",
            "workspace_name": "Example Workspace",
        }

    def test_malformed_json_is_structured_failure(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(0, "{not json")
        ):
            result = render.workspace_current()

        assert result["status"] == "failed"
        assert result["error_code"] == "RENDER_CLI_ERROR"
        assert result["retryable"] is False

    def test_json_without_id_is_structured_failure(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(0, json.dumps({"name": "x"}))
        ):
            result = render.workspace_current()

        assert result["status"] == "failed"
        assert result["error_code"] == "RENDER_CLI_ERROR"

    def test_not_logged_in_is_auth_failed(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(1, "", _NOT_LOGGED_IN_STDERR)
        ):
            result = render.workspace_current()

        assert result["error_code"] == "RENDER_AUTH_FAILED"
        assert result["retryable"] is False


# ---------------------------------------------------------------------------
# blueprints validate
# ---------------------------------------------------------------------------


class TestValidateBlueprint:
    def test_argv_and_cwd(self, tmp_path):
        with patch.object(
            render.subprocess, "run", return_value=_proc(0, _VALID_JSON)
        ) as mock_run:
            render.validate_blueprint("render.yaml", "tea-abc", cwd=tmp_path)

        assert mock_run.call_args.args[0] == [
            "render",
            "blueprints",
            "validate",
            "render.yaml",
            "-w",
            "tea-abc",
            "-o",
            "json",
        ]
        assert mock_run.call_args.kwargs["cwd"] == tmp_path

    def test_valid_true_is_success(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(0, _VALID_JSON)
        ):
            result = render.validate_blueprint("render.yaml", "tea-abc")

        assert result == {
            "status": "success",
            "valid": True,
            "plan": {
                "databases": ["u4smoke-db"],
                "services": ["u4smoke"],
                "totalActions": 2,
            },
        }

    def test_valid_false_is_blueprint_invalid_with_errors(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(1, _INVALID_JSON)
        ):
            result = render.validate_blueprint("render.yaml", "tea-abc")

        assert result["status"] == "failed"
        assert result["error_code"] == "RENDER_BLUEPRINT_INVALID"
        assert result["retryable"] is False
        assert result["exit_code"] == 1
        assert result["errors"] == json.loads(_INVALID_JSON)["errors"]
        assert "services[0].plan" in result["error_message"]
        assert "not-a-plan not a valid plan" in result["error_message"]

    def test_404_text_is_validate_failed_non_retryable(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(1, "", _404_STDERR)
        ):
            result = render.validate_blueprint("render.yaml", "tea-doesnotexist000")

        assert result["status"] == "failed"
        assert result["error_code"] == "RENDER_VALIDATE_FAILED"
        assert result["retryable"] is False
        assert "status 404" in result["error_message"]

    def test_401_text_is_validate_failed_non_retryable(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(1, "", _401_STDERR)
        ):
            result = render.validate_blueprint("render.yaml", "tea-abc")

        assert result["error_code"] == "RENDER_VALIDATE_FAILED"
        assert result["retryable"] is False

    def test_file_not_found_is_validate_failed_non_retryable(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(1, "", _FILE_NOT_FOUND_STDERR)
        ):
            result = render.validate_blueprint("render.yaml", "tea-abc")

        assert result["error_code"] == "RENDER_VALIDATE_FAILED"
        assert result["retryable"] is False

    def test_503_text_is_validate_failed_retryable(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(1, "", _503_STDERR)
        ):
            result = render.validate_blueprint("render.yaml", "tea-abc")

        assert result["error_code"] == "RENDER_VALIDATE_FAILED"
        assert result["retryable"] is True

    def test_timeout_is_validate_failed_retryable(self):
        with patch.object(
            render.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=["render"], timeout=60),
        ):
            result = render.validate_blueprint("render.yaml", "tea-abc")

        assert result["error_code"] == "RENDER_VALIDATE_FAILED"
        assert result["retryable"] is True
        assert result["exit_code"] is None

    def test_missing_binary_is_cli_missing(self):
        with patch.object(
            render.subprocess, "run", side_effect=FileNotFoundError("render")
        ):
            result = render.validate_blueprint("render.yaml", "tea-abc")

        assert result["error_code"] == "RENDER_CLI_MISSING"
        assert result["retryable"] is False

    def test_exit_zero_non_json_is_validate_failed(self):
        with patch.object(
            render.subprocess, "run", return_value=_proc(0, "Blueprint is valid")
        ):
            result = render.validate_blueprint("render.yaml", "tea-abc")

        assert result["status"] == "failed"
        assert result["error_code"] == "RENDER_VALIDATE_FAILED"
        assert result["retryable"] is False


# ---------------------------------------------------------------------------
# AGENTIC_CORE_DEBUG logging
# ---------------------------------------------------------------------------


def test_debug_logs_argv_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(render, "_DEBUG", True)
    with patch.object(render.subprocess, "run", return_value=_proc(0, _WHOAMI_JSON)):
        render.whoami()

    err = capsys.readouterr().err
    assert "[render-debug] $ render whoami -o json" in err
    assert "exit=0" in err
