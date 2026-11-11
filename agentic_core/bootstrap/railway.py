"""``railway`` CLI wrapper for the bootstrap orchestrator.

Three narrow operations the scaffold needs: ``create_railway_project``,
``set_env_var``, and ``deploy``. The interface is the same as
:mod:`agentic_core.bootstrap.github` — structured ``dict`` returns, no
escaping tracebacks, secrets stripped from anything we serialize.

The critical contract this module owns is the ``--railway-workspace``
thread-through (see spec §A4, §Cross-Track):

- ``workspace="agent-sandbox"`` (agent invocation) → ``railway init -n <name> -w agent-sandbox``
- ``workspace=None`` (the manual path) → ``railway init -n <name>`` (no -w)

The wrapper does not validate the workspace name — that's
the agent platform's job (project-ID allowlists per
workspace). We only mirror the flag through.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: When AGENTIC_CORE_DEBUG is truthy, every `railway` invocation logs its exact
#: argv, cwd, exit code, and FULL stdout/stderr to stderr. This is the escape
#: hatch for diagnosing terse Railway CLI errors ("prefix not found", etc.)
#: without guessing at what command actually ran.
_DEBUG = os.environ.get("AGENTIC_CORE_DEBUG", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)


def _run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` wrapper that debug-logs the exact command + output."""
    result = subprocess.run(*args, **kwargs)
    if _DEBUG:
        argv = args[0] if args else kwargs.get("args", [])
        printable = " ".join(str(a) for a in argv)
        print(f"[railway-debug] $ {printable}", file=sys.stderr)
        print(
            f"[railway-debug]   cwd={kwargs.get('cwd')!r} exit={result.returncode}",
            file=sys.stderr,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if out:
            print(f"[railway-debug]   stdout: {out}", file=sys.stderr)
        if err:
            print(f"[railway-debug]   stderr: {err}", file=sys.stderr)
    return result

DEFAULT_TIMEOUT = 60
DEPLOYMENT_SUCCESS_STATUSES = frozenset({"SUCCESS"})
DEPLOYMENT_FAILURE_STATUSES = frozenset(
    {"FAILED", "CRASHED", "CANCELED", "CANCELLED", "REMOVED", "SKIPPED"}
)


def _classify_railway_error(stderr: str) -> str:
    lower = stderr.lower()
    if "unauthorized" in lower or "not logged in" in lower or "railway login" in lower:
        return "RAILWAY_AUTH_FAILED"
    return "RAILWAY_CLI_ERROR"


def _is_project_absent(stderr: str) -> bool:
    """Conservative detection of an already-deleted / nonexistent project.

    Only clear "not found" / "does not exist" stderr counts — genuine
    auth/other failures must still surface as errors (Fix #17). This mirrors
    the Logto deletes' ``allow_missing`` tolerance so re-running teardown
    after the project is already gone is a no-op success, not a false failure.
    """
    lower = stderr.lower()
    return "not found" in lower or "does not exist" in lower or "doesn't exist" in lower


def _is_service_present(stderr: str) -> bool:
    """Conservative detection of a service that already exists.

    Mirrors :func:`_is_project_absent`: only a clear "already exists" / "already
    in use" stderr counts so a re-run / resume can ADOPT the existing service
    (create-service is idempotent) instead of failing. Genuine auth/other errors
    must still surface as failures.
    """
    lower = stderr.lower()
    return "already exists" in lower or "already in use" in lower


def _fail(*, error_code: str, exit_code: int | None, message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "error_code": error_code,
        "exit_code": exit_code,
        "error_message": message,
    }


# ---------------------------------------------------------------------------
# `--json` output parsing
#
# `--json` is a GLOBAL Railway CLI flag (verified — works on init/status/
# domain/up/add), but the per-command JSON *schema* is undocumented. The
# helpers below therefore parse DEFENSIVELY: they probe a small set of
# plausible key paths and never raise on a shape mismatch — a malformed or
# unexpected payload degrades to "field not found" rather than a crash.
#
# WARNING (plan Risk R-a): the exact key paths encoded here are ASSUMPTIONS.
# They MUST be validated against real `railway <cmd> --json` output during the
# first live bootstrap and tightened once the true schema is observed.
# ---------------------------------------------------------------------------


def _loads(stdout: str) -> Any | None:
    """Parse ``stdout`` as JSON, returning ``None`` on empty/invalid input."""
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_project_id(data: Any) -> str | None:
    """Best-effort extraction of a Railway project id from parsed --json output.

    Probes ``data["project"]["id"]`` first (the assumed nested shape), then a
    few flat fallbacks. Returns ``None`` if nothing project-id-shaped is found.
    """
    if not isinstance(data, dict):
        return None
    project = data.get("project")
    if isinstance(project, dict):
        pid = project.get("id")
        if pid:
            return str(pid)
    for key in ("projectId", "project_id", "id"):
        val = data.get(key)
        if val:
            return str(val)
    return None


def _extract_project_name(data: Any) -> str | None:
    """Best-effort extraction of the linked Railway project's NAME.

    Mirrors :func:`_extract_project_id`'s tolerance. Probes the nested
    ``data["project"]["name"]`` first, then a few flat fallbacks. Returns
    ``None`` if nothing name-shaped is found.

    WARNING (Risk R-a): the ``railway status --json`` schema is UNDOCUMENTED —
    the exact key that carries the project name here is ASSUMED
    (``project.name`` / top-level ``name``) and MUST be validated against real
    ``railway status --json`` output before this is trusted in production. If
    the live shape differs, this returns ``None``; the orchestrator treats an
    unextractable name as an UNVERIFIABLE match and refuses to adopt the linked
    project (fails ``RAILWAY_PROJECT_MISMATCH``) rather than silently
    provisioning against the wrong project.
    """
    if not isinstance(data, dict):
        return None
    project = data.get("project")
    if isinstance(project, dict):
        name = project.get("name")
        if name:
            return str(name)
    for key in ("name", "projectName", "project_name"):
        val = data.get(key)
        if val:
            return str(val)
    return None


def _extract_service_id(data: Any) -> str | None:
    """Best-effort extraction of the first service id, if present."""
    if not isinstance(data, dict):
        return None
    # A singular ``service`` dict (the plausible ``railway add --service --json``
    # shape) — probed first so create_railway_service can surface the new id.
    service = data.get("service")
    if isinstance(service, dict) and service.get("id"):
        return str(service["id"])
    project = data.get("project")
    containers = [project] if isinstance(project, dict) else [data]
    for container in containers:
        if not isinstance(container, dict):
            continue
        services = container.get("services")
        if isinstance(services, list) and services:
            first = services[0]
            if isinstance(first, dict) and first.get("id"):
                return str(first["id"])
        for key in ("serviceId", "service_id"):
            val = container.get(key)
            if val:
                return str(val)
    return None


def _extract_domain(data: Any) -> str | None:
    """Best-effort extraction of the assigned prod hostname from parsed
    ``railway domain --json`` output.

    ``domain --json`` schema is undocumented (Risk R-a); it is plausibly a flat
    ``{"domain": "<host>"}``, a ``{"domains": [...]}`` / ``{"serviceDomains":
    [...]}`` list (of strings OR of ``{"domain": ...}`` dicts), or a bare list.
    We probe those paths and normalize to a BARE hostname (scheme + trailing
    path stripped) so the caller can wrap it as ``https://<host>/callback`` and
    ``https://<host>`` (CORS origin) without double-scheming. Returns ``None``
    on any non-matching shape — the corrupted decorated-stdout that broke an
    early live run parses to ``None`` here, not a bogus domain.
    """
    if isinstance(data, dict):
        for key in ("domain", "url", "host", "hostname"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return _normalize_domain(val)
            if isinstance(val, dict):
                inner = val.get("domain") or val.get("host") or val.get("hostname")
                if isinstance(inner, str) and inner.strip():
                    return _normalize_domain(inner)
        for key in ("domains", "serviceDomains", "customDomains"):
            found = _first_domain_in_seq(data.get(key))
            if found:
                return found
    if isinstance(data, list):
        found = _first_domain_in_seq(data)
        if found:
            return found
    return None


def _first_domain_in_seq(seq: Any) -> str | None:
    """Return the first hostname in a sequence of strings or ``{"domain": ...}``
    dicts, normalized. ``None`` if the sequence is empty / not a list."""
    if not isinstance(seq, list):
        return None
    for item in seq:
        if isinstance(item, str) and item.strip():
            return _normalize_domain(item)
        if isinstance(item, dict):
            inner = item.get("domain") or item.get("host") or item.get("hostname")
            if isinstance(inner, str) and inner.strip():
                return _normalize_domain(inner)
    return None


def _normalize_domain(value: str) -> str:
    """Strip a leading scheme and any trailing path/slash → bare hostname."""
    host = value.strip()
    for scheme in ("https://", "http://"):
        if host.startswith(scheme):
            host = host[len(scheme):]
            break
    # Drop any path component (``host/foo`` → ``host``) and trailing slash.
    host = host.split("/", 1)[0]
    return host.rstrip("/")


def get_prod_domain(
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
    service: str | None = None,
) -> dict[str, Any]:
    """Capture the service's assigned prod domain via ``railway domain --json``.

    This is the domain-capture half of STEP_WIRING (U6). An
    early live-run incident came from parsing the DECORATED ``railway
    domain`` stdout (``ServiceDomain created 🚀 https://…``) — so we use the
    global ``--json`` flag and the tolerant :func:`_extract_domain` parse layer
    instead, and refuse a zero-exit whose ``--json`` body doesn't yield a
    domain (rather than silently trusting un-parsed text).

    ``railway domain`` returns the existing service domain, generating one on
    first call — so it is callable once the service exists (post ``railway
    init``), before the deploy.

    Returns
    -------
    dict
        Success: ``{"status": "success", "domain": "<bare-hostname>"}``.
        Failure: structured failure envelope (``RAILWAY_CLI_ERROR`` for an
        unparseable ``--json`` body on an otherwise-zero exit).
    """
    argv: list[str] = ["railway", "domain", "--json"]
    if service:
        argv.extend(["-s", service])

    try:
        result = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="RAILWAY_TIMEOUT",
            exit_code=None,
            message=f"railway domain timeout after {timeout}s",
        )

    if result.returncode != 0:
        return _fail(
            error_code=_classify_railway_error(result.stderr),
            exit_code=result.returncode,
            message=result.stderr.strip(),
        )

    data = _loads(result.stdout)
    domain = _extract_domain(data)
    if domain is None:
        # Exit 0 but no parseable domain — refuse to record a domain we can't
        # verify (same anti-pattern guard as create_railway_project; this is
        # exactly the decorated-stdout failure class U6 exists to kill).
        return _fail(
            error_code="RAILWAY_CLI_ERROR",
            exit_code=result.returncode,
            message=(
                "railway domain succeeded but --json output could not be "
                "parsed for a domain"
            ),
        )

    return {"status": "success", "domain": domain}


def create_railway_project(
    name: str,
    workspace: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Create a new Railway project via ``railway init``.

    Parameters
    ----------
    name : str
        Project name. Becomes the Railway dashboard label and, by default,
        the service name.
    workspace : str | None
        If a non-empty string, ``-w <workspace>`` is appended to argv.
        ``None`` or empty string ⇒ flag omitted; Railway falls back to
        the CLI's default workspace (preserves the manual path).
    timeout : int
        Subprocess timeout in seconds. Default 60.

    Returns
    -------
    dict
        Success: ``{"status": "success", "name": <name>, "workspace":
        <workspace-or-None>, "project_id": <uuid-or-None>, "service_id":
        <uuid-or-None>}``. The ``project_id`` is parsed from ``railway init
        --json`` and persisted to bootstrap state by the orchestrator (IDs
        only — never secrets).
        Failure: structured failure envelope. A malformed/empty ``--json``
        payload on an otherwise-zero exit is reported as ``RAILWAY_CLI_ERROR``
        rather than silently succeeding (the decorated-stdout parsing that
        corrupted an early live run is the failure class we're
        killing).
    """
    # `--json` is a global flag; we parse structured output instead of the
    # decorated stdout the old client ignored entirely.
    argv: list[str] = ["railway", "init", "-n", name, "--json"]
    # `workspace=""` (e.g. from `--railway-workspace ""` on the shell) is
    # treated as "no workspace specified". Avoids `railway init -n foo -w ""`.
    if workspace:
        argv.extend(["-w", workspace])

    try:
        result = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="RAILWAY_TIMEOUT",
            exit_code=None,
            message=f"railway init timeout after {timeout}s",
        )

    if result.returncode != 0:
        return _fail(
            error_code=_classify_railway_error(result.stderr),
            exit_code=result.returncode,
            message=result.stderr.strip(),
        )

    data = _loads(result.stdout)
    project_id = _extract_project_id(data)
    if project_id is None:
        # Exit code was 0 but we couldn't extract a project id from --json.
        # Fail loudly rather than record a success we can't verify — this is
        # the anti-pattern (trusting un-parsed CLI output) U3 exists to remove.
        return _fail(
            error_code="RAILWAY_CLI_ERROR",
            exit_code=result.returncode,
            message=(
                "railway init succeeded but --json output could not be parsed "
                "for a project id"
            ),
        )

    return {
        "status": "success",
        "name": name,
        "workspace": workspace or None,
        "project_id": project_id,
        "service_id": _extract_service_id(data),
    }


def create_railway_service(
    service: str,
    cwd: str | Path | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Create a service in the linked project via ``railway add --service <name>``.

    ``railway init`` creates an EMPTY project with NO service, so the very next
    ``railway variables --set`` fails with "Project has no services". Railway's
    model requires: init → CREATE A SERVICE → set variables on it → deploy to
    it. This is the missing create-service step.

    Idempotent: a service that already exists (re-run / resume) is tolerated as a
    success flagged ``adopted`` — mirroring :func:`delete_project`'s
    already-absent handling — so a resume never fails on a service the first run
    already made.

    Parameters
    ----------
    service : str
        Service name to create. The orchestrator uses the app slug (same as the
        project name).
    cwd : str | Path | None
        Working directory. Must be the directory linked to the target project so
        the service lands in the right project.
    timeout : int
        Subprocess timeout in seconds. Default 60.

    Returns
    -------
    dict
        Success: ``{"status": "success", "service": <service>, "service_id":
        <uuid-or-None>}`` (plus ``"adopted": True`` when the service already
        existed). Failure: structured failure envelope.
    """
    argv: list[str] = ["railway", "add", "--service", service, "--json"]

    try:
        result = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="RAILWAY_TIMEOUT",
            exit_code=None,
            message=f"railway add --service {service} timeout after {timeout}s",
        )

    if result.returncode != 0:
        # Idempotency: an already-present service is a tolerated adopt (resume /
        # re-run), NOT a failure — conservative, only clear "already exists"
        # stderr counts; genuine auth/other errors still fail.
        if _is_service_present(result.stderr):
            return {
                "status": "success",
                "service": service,
                "service_id": None,
                "adopted": True,
            }
        return _fail(
            error_code=_classify_railway_error(result.stderr),
            exit_code=result.returncode,
            message=result.stderr.strip(),
        )

    # Zero exit = service created. The ``add`` --json schema is undocumented
    # (Risk R-a) so the service id is best-effort: a missing id degrades to None
    # rather than failing the step (unlike create_railway_project's project_id,
    # which gates success — here the zero exit already confirms creation).
    data = _loads(result.stdout)
    return {
        "status": "success",
        "service": service,
        "service_id": _extract_service_id(data),
    }


def get_linked_project(
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect the currently linked Railway project via ``railway status --json``.

    This is the ``check()`` half of the step contract for the Railway step:
    a resume can ADOPT an already-linked project instead of re-running
    ``railway init`` (which is not idempotent and errors on re-run — the root
    cause of the manual state-file repair an early live run
    required).

    Intentionally non-fatal: any condition under which a linked project can't
    be confirmed (no link, non-zero exit, unparseable JSON, timeout) returns
    ``linked=False`` so the caller falls through to ``init`` rather than
    crashing. It never returns a failure envelope.

    Returns
    -------
    dict
        ``{"status": "success", "linked": True, "project_id": <uuid>,
        "name": <project-name-or-None>, "service_id": <uuid-or-None>}`` when a
        project is linked, else ``{"status": "success", "linked": False}``.

    ``name`` lets the caller VERIFY the linked project matches the app slug
    before adopting it — silently adopting a cwd that happens to be linked to a
    *different* Railway project is the worst outcome (env vars / Postgres /
    deploys land on the wrong project). ``name`` is ``None`` when it can't be
    extracted (see :func:`_extract_project_name`); the caller treats that as a
    non-match.
    """
    argv: list[str] = ["railway", "status", "--json"]
    try:
        result = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        # Can't determine linkage — treat as "not linked" so the caller runs
        # init. (Worst case the caller's own init surfaces a real error.)
        return {"status": "success", "linked": False}

    if result.returncode != 0:
        # `railway status` exits non-zero when no project is linked.
        return {"status": "success", "linked": False}

    data = _loads(result.stdout)
    project_id = _extract_project_id(data)
    if project_id is None:
        return {"status": "success", "linked": False}

    return {
        "status": "success",
        "linked": True,
        "project_id": project_id,
        "name": _extract_project_name(data),
        "service_id": _extract_service_id(data),
    }


def _detect_database_url(data: Any) -> bool:
    """Best-effort: does parsed ``railway variables --json`` expose a DATABASE_URL?

    ``variables --json`` schema is undocumented (KTD4 / Risk R-a); it is
    plausibly a flat ``{"NAME": "value", ...}`` map or a per-service nesting.
    We only ever test for the *presence* of a ``DATABASE_URL`` key — never read
    or return its (secret-shaped) value. Returns ``False`` on any non-dict.
    """
    if not isinstance(data, dict):
        return False
    if "DATABASE_URL" in data:
        return True
    for val in data.values():
        if isinstance(val, dict) and "DATABASE_URL" in val:
            return True
    return False


def has_database(
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Detect an existing Postgres/DATABASE_URL via ``railway variables --json``.

    This is the ``check()`` half of the Postgres step (U5 / KTD4): a resume
    must ADOPT an already-provisioned database instead of running
    ``railway add`` again — ``add`` is not idempotent and would provision a
    *second* Postgres service (plan Risk R-c).

    Intentionally non-fatal, mirroring :func:`get_linked_project`: any
    condition under which a DB can't be confirmed (non-zero exit, unparseable
    JSON, timeout) returns ``has_database=False`` so the caller falls through
    to :func:`add_database`. It never returns a failure envelope and never
    echoes the connection string.

    Returns
    -------
    dict
        ``{"status": "success", "has_database": <bool>}``.
    """
    argv: list[str] = ["railway", "variables", "--json"]
    try:
        result = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        # Can't determine DB presence — treat as "no DB" so the caller adds one.
        return {"status": "success", "has_database": False}

    if result.returncode != 0:
        # `railway variables` exits non-zero with no linked project/service.
        return {"status": "success", "has_database": False}

    data = _loads(result.stdout)
    return {"status": "success", "has_database": _detect_database_url(data)}


def add_database(
    kind: str = "postgres",
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Provision a database via ``railway add --database <kind> --json``.

    Per KTD4, ``railway add --database postgres`` is non-interactive (the DB
    type is supplied, so the CLI does not prompt), auto-links the new service
    to the linked project, auto-deploys it, and exposes ``DATABASE_URL`` in the
    service env — which the template's ``preDeployCommand: alembic upgrade
    head`` then consumes. We do NOT pass ``--yes`` because ``--database`` already
    makes the command non-interactive; add it here only if a future CLI version
    reintroduces a prompt.

    ``--json`` is a global flag; as in :func:`create_railway_project`, an empty
    or malformed ``--json`` payload on an otherwise-zero exit is reported as
    ``RAILWAY_CLI_ERROR`` rather than a silent success — the decorated-stdout
    parsing that corrupted an early live run is the failure class
    U3/U5 exist to kill (Risk R-a). Any valid JSON body is treated as success;
    we do not require a specific field because the ``add`` schema is
    undocumented, and we deliberately never surface ``DATABASE_URL`` (a
    secret-shaped connection string) in the return dict.

    Returns
    -------
    dict
        Success: ``{"status": "success", "kind": <kind>}`` (no connection
        string). Failure: structured failure envelope.
    """
    argv: list[str] = ["railway", "add", "--database", kind, "--json"]

    try:
        result = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="RAILWAY_TIMEOUT",
            exit_code=None,
            message=f"railway add --database {kind} timeout after {timeout}s",
        )

    if result.returncode != 0:
        return _fail(
            error_code=_classify_railway_error(result.stderr),
            exit_code=result.returncode,
            message=result.stderr.strip(),
        )

    data = _loads(result.stdout)
    if data is None:
        # Exit 0 but empty/unparseable --json — refuse to record a success we
        # can't verify (same anti-pattern guard as create_railway_project).
        return _fail(
            error_code="RAILWAY_CLI_ERROR",
            exit_code=result.returncode,
            message=(
                f"railway add --database {kind} succeeded but --json output "
                "could not be parsed"
            ),
        )

    # Success. Intentionally omit any DATABASE_URL / connection string from the
    # return dict — it is secret-shaped and must never reach state or logs.
    return {"status": "success", "kind": kind}


def set_env_var(
    name: str,
    value: str,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
    service: str | None = None,
) -> dict[str, Any]:
    """Set a single Railway env var via ``railway variables --set NAME=VALUE``.

    The value DOES appear on the command line (Railway CLI's documented
    contract is ``--set NAME=VALUE``; there's no stdin alternative). The
    returned dict, however, never echoes the value — callers can serialize
    it safely.

    Parameters
    ----------
    name : str
        Env var name.
    value : str
        Env var value. Not echoed in the return dict.
    timeout : int
        Subprocess timeout in seconds.
    service : str | None
        When set, ``-s <service>`` targets a specific service. ``railway
        variables`` REQUIRES this once the project has multiple services (e.g.
        the app service + Postgres), so the orchestrator always passes it.

    Returns
    -------
    dict
        Success: ``{"status": "success", "name": <name>}`` (value omitted).
        Failure: structured failure envelope (also no value).
    """
    argv: list[str] = ["railway", "variables", "--set", f"{name}={value}"]
    if service:
        argv.extend(["-s", service])
    # ALWAYS skip the deploy each var-set would otherwise trigger — the pipeline
    # deploys exactly once at the end (STEP_RAILWAY_DEPLOY), not per var.
    argv.append("--skip-deploys")

    try:
        result = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="RAILWAY_TIMEOUT",
            exit_code=None,
            message=f"railway variables --set timeout after {timeout}s",
        )

    if result.returncode != 0:
        # stderr is unlikely to echo the value, but redact defensively.
        return _fail(
            error_code=_classify_railway_error(result.stderr),
            exit_code=result.returncode,
            message=_redact(result.stderr, value).strip(),
        )

    return {
        "status": "success",
        "name": name,
    }


def link_project(
    project_id: str,
    service: str,
    environment: str = "production",
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Link ``cwd`` to a project + service + environment via ``railway link``.

    REQUIRED before :func:`deploy`. ``railway up`` deploys the code of, and
    resolves the project from, the CURRENT DIRECTORY's link. ``railway init``
    linked the orchestrator's cwd, NOT the rendered app dir — so a bare
    ``railway up`` from the app dir fails with "prefix not found" / "No linked
    project found". Passing all targets as flags makes ``railway link``
    non-interactive. (Validated live against the real Railway CLI.)
    """
    argv: list[str] = [
        "railway",
        "link",
        "-p",
        project_id,
        "-s",
        service,
        "-e",
        environment,
    ]
    try:
        result = _run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="RAILWAY_TIMEOUT",
            exit_code=None,
            message=f"railway link timeout after {timeout}s",
        )
    if result.returncode != 0:
        return _fail(
            error_code=_classify_railway_error(result.stderr),
            exit_code=result.returncode,
            message=result.stderr.strip(),
        )
    return {"status": "success", "project_id": project_id, "service": service}


def deploy(
    detached: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
    service: str | None = None,
) -> dict[str, Any]:
    """Deploy the linked app dir via ``railway up -d``.

    The caller MUST link ``cwd`` to the project + service first (see
    :func:`link_project`) — ``railway up`` resolves the project from the
    CURRENT DIRECTORY's link and deploys that directory's code. With the link
    in place, a bare ``railway up -d`` from the app dir deploys correctly
    (validated live). ``-s <service>`` is redundant with the link but passed
    for explicitness.

    Parameters
    ----------
    detached : bool
        ``True`` (default) ⇒ ``railway up -d`` (returns immediately after
        Railway accepts the build). ``False`` blocks until the build finishes.
    timeout : int
        Bounds the CLI call, not the deploy itself. 60s is plenty for detached.

    Returns
    -------
    dict
        Success: ``{"status": "success"}``. Failure: structured failure envelope.
    """
    argv: list[str] = ["railway", "up"]
    if detached:
        argv.append("-d")
    if service:
        argv.extend(["-s", service])

    try:
        result = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="RAILWAY_TIMEOUT",
            exit_code=None,
            message=f"railway up timeout after {timeout}s",
        )

    if result.returncode != 0:
        return _fail(
            error_code=_classify_railway_error(result.stderr),
            exit_code=result.returncode,
            message=result.stderr.strip(),
        )

    return {"status": "success"}


def wait_for_deployment(
    *,
    service: str,
    cwd: str | Path | None = None,
    timeout: float = 600,
    poll_interval: float = 5,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Wait for the latest Railway deployment to reach a terminal state.

    ``railway up -d`` only confirms upload acceptance. Without this poll, a
    failed Docker build is mistaken for a slow rollout and the orchestrator
    wastes its health-check budget polling an app that cannot start.
    """
    deadline = time.monotonic() + timeout
    last_status: str | None = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _fail(
                error_code="RAILWAY_DEPLOY_TIMEOUT",
                exit_code=None,
                message=(
                    f"Railway deployment for service {service!r} did not reach "
                    f"a terminal state within {timeout:g}s "
                    f"(last: {last_status or 'unknown'})"
                ),
            )

        try:
            result = _run(
                ["railway", "deployment", "list", "--service", service, "--json"],
                capture_output=True,
                text=True,
                timeout=min(DEFAULT_TIMEOUT, max(1, remaining)),
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            return _fail(
                error_code="RAILWAY_TIMEOUT",
                exit_code=None,
                message="railway deployment list timed out",
            )

        if result.returncode != 0:
            return _fail(
                error_code=_classify_railway_error(result.stderr),
                exit_code=result.returncode,
                message=result.stderr.strip(),
            )

        data = _loads(result.stdout)
        latest = data[0] if isinstance(data, list) and data else None
        if not isinstance(latest, dict) or not latest.get("status"):
            return _fail(
                error_code="RAILWAY_CLI_ERROR",
                exit_code=result.returncode,
                message="railway deployment list returned no parseable deployment",
            )

        deployment_id = str(latest.get("id") or "")
        deployment_status = str(latest["status"]).upper()
        if deployment_status != last_status:
            last_status = deployment_status
            if on_status is not None:
                on_status(deployment_status)

        if deployment_status in DEPLOYMENT_SUCCESS_STATUSES:
            return {
                "status": "success",
                "deployment_id": deployment_id,
                "deployment_status": deployment_status,
            }
        if deployment_status in DEPLOYMENT_FAILURE_STATUSES:
            return _fail(
                error_code="RAILWAY_DEPLOY_FAILED",
                exit_code=3,
                message=(
                    f"Railway deployment {deployment_id or '<unknown>'} for "
                    f"service {service!r} reached {deployment_status}. Run "
                    f"`railway logs --build {deployment_id} --lines 200` for details."
                ),
            )

        if poll_interval > 0:
            time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))


def delete_project(
    project_id: str | None = None,
    name: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """Delete a Railway project via ``railway delete --project <id> --yes --json``.

    The U8 teardown escape hatch: removes the per-app Railway project a
    bootstrap run created. Prefers ``project_id`` (the UUID persisted to
    state); falls back to ``name`` — the CLI's ``--project`` accepts either.

    ``--yes`` is ALWAYS passed so the Railway CLI never blocks on its own
    interactive confirmation. This is not the operator's consent — the
    operator-facing confirmation lives one layer up in
    :func:`agentic_core.bootstrap.teardown.teardown_app` (KTD5). ``--yes``
    here only makes the subprocess non-interactive so an already-confirmed
    teardown can run unattended.

    Railway schedules deletion asynchronously (per its CLI automation notes:
    "treat a successful response as the deletion request being accepted"), so
    a zero exit is success regardless of whether the ``--json`` body parses —
    unlike ``railway init`` we do NOT require a structured payload.

    Non-interactive-delete limitation: ``railway delete`` cannot verify the
    deletion completed (it's async) and, if the account enables 2FA, requires
    a ``--2fa-code`` we don't supply — a 2FA-gated account must run the
    delete manually. Both are acceptable for a manual escape hatch.

    Returns
    -------
    dict
        Success: ``{"status": "success", "project": <target>}``.
        Failure: structured failure envelope.
    """
    target = project_id or name
    if not target:
        return _fail(
            error_code="RAILWAY_CLI_ERROR",
            exit_code=None,
            message="delete_project requires a project_id or name",
        )

    argv: list[str] = ["railway", "delete", "--project", str(target), "--yes", "--json"]

    try:
        result = _run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return _fail(
            error_code="RAILWAY_TIMEOUT",
            exit_code=None,
            message=f"railway delete timeout after {timeout}s",
        )

    if result.returncode != 0:
        # Fix #17: already-deleted / nonexistent projects are a tolerated no-op
        # (re-running teardown after the project is gone), mirroring the Logto
        # deletes' allow_missing. Be conservative — only clear "not found" /
        # "does not exist" stderr counts; genuine auth/other errors still fail.
        if _is_project_absent(result.stderr):
            return {
                "status": "success",
                "project": str(target),
                "already_absent": True,
            }
        return _fail(
            error_code=_classify_railway_error(result.stderr),
            exit_code=result.returncode,
            message=result.stderr.strip(),
        )

    # Deletion is async — a zero exit means the request was accepted. We do
    # NOT require the --json body to parse (it may be empty).
    return {"status": "success", "project": str(target)}


def _redact(text: str, secret: str) -> str:
    """Replace any occurrence of ``secret`` in ``text`` with ``***``.

    Used to scrub a known secret value out of subprocess error output
    before it lands in a returned dict. Short / empty secrets are left
    alone (the masking would be more noise than signal).
    """
    if not secret or len(secret) < 4:
        return text
    return text.replace(secret, "***")
