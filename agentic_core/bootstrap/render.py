"""``render`` CLI wrapper for the bootstrap orchestrator (U4 / KTD2).

Three read-only operations the Render deploy target needs: ``whoami``,
``workspace_current``, and ``validate_blueprint``. None of them creates a
Render resource. The interface mirrors :mod:`agentic_core.bootstrap.railway`:
structured ``dict`` returns, no escaping tracebacks, JSON parsing only
(``-o json``), and argv logging under ``AGENTIC_CORE_DEBUG``.

Every failure dict carries a ``retryable`` boolean so the orchestrator can
tell a transient Render API problem (5xx, timeout) from a structural one
(logged out, bad workspace, invalid Blueprint).

Output shapes below were observed with Render CLI v2.31.0:

- ``render whoami -o json`` → ``{"email", "id", "name"}``, exit 0.
- ``render workspace current -o json`` → ``{"email", "id", "name", "type"}``.
- ``render blueprints validate <file> -w <id> -o json``:
  valid → exit 0, ``{"valid": true, "plan": {...}}``;
  invalid → exit 1, ``{"valid": false, "errors": [{"path", "error", "line",
  "column"}]}``; API or file errors → exit 1, empty stdout, and plain text on
  stderr such as ``Error: validation request failed with status 404: ...`` or
  ``Error: file not found: <path>``.
- Logged out → exit 1, stderr ``... run `render login` to authenticate`` (or
  ``your token is expired; run `render login` ...``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

#: When AGENTIC_CORE_DEBUG is truthy, every `render` invocation logs its exact
#: argv, cwd, exit code, and full stdout/stderr to stderr (same escape hatch as
#: the railway wrapper).
_DEBUG = os.environ.get("AGENTIC_CORE_DEBUG", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

DEFAULT_TIMEOUT = 60

#: ``status 5xx`` in the CLI's plain-text API error → a transient Render fault.
_STATUS_5XX = re.compile(r"\bstatus 5\d\d\b")


def _run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` wrapper that debug-logs the exact command + output."""
    result = subprocess.run(*args, **kwargs)
    if _DEBUG:
        argv = args[0] if args else kwargs.get("args", [])
        printable = " ".join(str(a) for a in argv)
        print(f"[render-debug] $ {printable}", file=sys.stderr)
        print(
            f"[render-debug]   cwd={kwargs.get('cwd')!r} exit={result.returncode}",
            file=sys.stderr,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if out:
            print(f"[render-debug]   stdout: {out}", file=sys.stderr)
        if err:
            print(f"[render-debug]   stderr: {err}", file=sys.stderr)
    return result


def _fail(
    *,
    error_code: str,
    exit_code: int | None,
    message: str,
    retryable: bool,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "error_code": error_code,
        "exit_code": exit_code,
        "error_message": message,
        "retryable": retryable,
    }


def _loads(stdout: str) -> Any | None:
    """Parse ``stdout`` as JSON, returning ``None`` on empty/invalid input."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _is_auth_error(text: str) -> bool:
    lower = text.lower()
    return (
        "render login" in lower
        or "not logged in" in lower
        or "unauthorized" in lower
        or "status 401" in lower
    )


def _is_transient(text: str) -> bool:
    return bool(_STATUS_5XX.search(text.lower()))


def _missing() -> dict[str, Any]:
    return _fail(
        error_code="RENDER_CLI_MISSING",
        exit_code=None,
        message="`render` binary not found on PATH (install the Render CLI).",
        retryable=False,
    )


def _invoke(
    argv: list[str], *, timeout: int, cwd: str | Path | None = None
) -> subprocess.CompletedProcess[str] | dict[str, Any]:
    """Run ``argv``; return the process, or a failure dict for a missing binary
    or a timeout. Timeout failures use ``RENDER_TIMEOUT``; callers may recode."""
    try:
        return _run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        return _missing()
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="RENDER_TIMEOUT",
            exit_code=None,
            message=f"`{' '.join(argv[:3])}` timed out after {timeout}s",
            retryable=True,
        )


def _cli_failure(result: subprocess.CompletedProcess[str], command: str) -> dict[str, Any]:
    """Classify a non-zero exit of ``whoami`` / ``workspace current``."""
    detail = (result.stderr or result.stdout or "").strip()
    if _is_auth_error(detail):
        return _fail(
            error_code="RENDER_AUTH_FAILED",
            exit_code=result.returncode,
            message=f"`{command}` failed — run `render login`. {detail}".strip(),
            retryable=False,
        )
    return _fail(
        error_code="RENDER_CLI_ERROR",
        exit_code=result.returncode,
        message=detail or f"`{command}` exited {result.returncode}",
        retryable=_is_transient(detail),
    )


def whoami(timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Check Render CLI login via ``render whoami -o json``.

    Success: ``{"status": "success", "email", "user_id", "name"}``.
    """
    command = "render whoami"
    result = _invoke(["render", "whoami", "-o", "json"], timeout=timeout)
    if isinstance(result, dict):
        return result
    if result.returncode != 0:
        return _cli_failure(result, command)
    data = _loads(result.stdout)
    if not isinstance(data, dict) or not data.get("email"):
        return _fail(
            error_code="RENDER_CLI_ERROR",
            exit_code=result.returncode,
            message=f"`{command} -o json` returned no parseable user",
            retryable=False,
        )
    return {
        "status": "success",
        "email": str(data["email"]),
        "user_id": str(data.get("id") or ""),
        "name": str(data.get("name") or ""),
    }


def workspace_current(timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Read the active workspace via ``render workspace current -o json``.

    Success: ``{"status": "success", "workspace_id", "workspace_name"}``.
    """
    command = "render workspace current"
    result = _invoke(["render", "workspace", "current", "-o", "json"], timeout=timeout)
    if isinstance(result, dict):
        return result
    if result.returncode != 0:
        return _cli_failure(result, command)
    data = _loads(result.stdout)
    if not isinstance(data, dict) or not data.get("id"):
        return _fail(
            error_code="RENDER_CLI_ERROR",
            exit_code=result.returncode,
            message=f"`{command} -o json` returned no workspace id",
            retryable=False,
        )
    return {
        "status": "success",
        "workspace_id": str(data["id"]),
        "workspace_name": str(data.get("name") or ""),
    }


def _format_errors(errors: list[Any]) -> str:
    parts: list[str] = []
    for item in errors:
        if isinstance(item, dict):
            path = item.get("path")
            msg = item.get("error") or item.get("message") or json.dumps(item)
            parts.append(f"{path}: {msg}" if path else str(msg))
        else:
            parts.append(str(item))
    return "; ".join(parts)


def validate_blueprint(
    path: str | Path,
    workspace_id: str,
    cwd: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Validate a Blueprint against Render with
    ``render blueprints validate <path> -w <workspace_id> -o json``.

    A live, read-only API call: it checks schema, plans, regions, and name
    conflicts in the workspace. It creates nothing.

    Classification (KTD2), from stdout JSON first:

    - ``valid: true`` with exit 0 → ``{"status": "success", "valid": True,
      "plan": {...}}``.
    - ``valid: false`` → ``RENDER_BLUEPRINT_INVALID``, non-retryable, with the
      CLI's ``errors`` list.
    - Non-JSON output → ``RENDER_VALIDATE_FAILED``. Retryable only for a 5xx
      status or a timeout; 401, 404, a missing file, and anything else are
      non-retryable.
    """
    argv = ["render", "blueprints", "validate", str(path), "-w", workspace_id, "-o", "json"]
    result = _invoke(argv, timeout=timeout, cwd=cwd)
    if isinstance(result, dict):
        if result["error_code"] == "RENDER_TIMEOUT":
            result["error_code"] = "RENDER_VALIDATE_FAILED"
        return result

    data = _loads(result.stdout)
    if isinstance(data, dict) and data.get("valid") is False:
        errors = data.get("errors")
        errors = errors if isinstance(errors, list) else []
        return {
            **_fail(
                error_code="RENDER_BLUEPRINT_INVALID",
                exit_code=result.returncode,
                message=_format_errors(errors) or "Render rejected the Blueprint",
                retryable=False,
            ),
            "errors": errors,
        }
    if isinstance(data, dict) and data.get("valid") is True and result.returncode == 0:
        plan = data.get("plan")
        return {
            "status": "success",
            "valid": True,
            "plan": plan if isinstance(plan, dict) else {},
        }

    detail = (result.stderr or result.stdout or "").strip()
    return _fail(
        error_code="RENDER_VALIDATE_FAILED",
        exit_code=result.returncode,
        message=detail or f"render blueprints validate exited {result.returncode}",
        retryable=_is_transient(detail),
    )
