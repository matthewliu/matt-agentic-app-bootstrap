"""Preflight credential validation for ``agentic-core create-app`` (§R6 / SC4).

Before the orchestrator touches ANY external system (keystore decrypt,
Logto, GitHub, Railway), :func:`run_preflight` verifies that every
credential, CLI login, token, and the admin keystore file are present and
usable. A run with a stale or missing credential is refused *up front* —
nothing gets created, so the failure is structural (``retryable=False``
when the orchestrator turns it into a §R3 :class:`FailureEnvelope`).

Checks (each gated on the same opt-out flags the orchestrator honours):

  - ``--github`` (default on): ``gh`` binary present + ``gh auth status`` OK.
  - ``--railway`` (default on, Railway target only): ``railway`` binary
    present + ``railway whoami`` OK + ``RAILWAY_API_TOKEN`` env present.
  - Render target with deploy enabled: ``render`` binary present +
    ``render whoami`` OK + ``render workspace current`` returns a workspace.
    The Railway checks are skipped for this target. With deploy disabled,
    neither the Render nor the Railway checks run.
  - ``--auth`` (Logto, default on): ``LOGTO_M2M_APP_ID`` (or an explicit
    ``admin_app_id``) + a Logto endpoint present, and the admin keystore
    file exists and is readable.

This module is pure validation — no side effects, no secret material read.
Binary presence uses :func:`shutil.which`; auth is a bounded
``subprocess.run`` (mirroring :mod:`agentic_core.bootstrap.railway` /
:mod:`agentic_core.bootstrap.github`: list argv, ``capture_output``,
``text``, ``timeout``, never ``shell=True``, no ``check=True``).

Distinct ``error_code`` per failure class so agent callers (and the
CLI's ``--preflight`` flag) can branch:

  - ``PREFLIGHT_GH_MISSING`` / ``PREFLIGHT_GH_AUTH_FAILED``
  - ``PREFLIGHT_RAILWAY_MISSING`` / ``PREFLIGHT_RAILWAY_AUTH_FAILED``
  - ``PREFLIGHT_ENV_MISSING``
  - ``PREFLIGHT_KEYSTORE_MISSING``
  - ``RENDER_CLI_MISSING`` / ``RENDER_AUTH_FAILED`` (and any other
    :mod:`agentic_core.bootstrap.render` code, passed through unchanged)

Exit codes mirror §R4: ``2`` (auth) for login/keystore failures, ``5``
(misconfig) for a missing binary, token, or env var.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from agentic_core.bootstrap import keystore, render

#: Bounded timeout for the auth-status subprocess calls (seconds). These are
#: local metadata reads for ``gh`` / ``railway`` — a few seconds is plenty.
DEFAULT_TIMEOUT = 15

#: §R4 exit-code classes reused here.
EXIT_AUTH = 2
EXIT_MISCONFIG = 5

#: Env var carrying the Logto endpoint (see ``.env.example`` / ``admin.py``).
ENV_LOGTO_ENDPOINT = "LOGTO_ENDPOINT"
#: Env var carrying the bootstrap admin M2M app id.
ENV_LOGTO_M2M_APP_ID = "LOGTO_M2M_APP_ID"
#: Env var carrying the domain of the Logto API identifiers.
ENV_PLATFORM_DOMAIN = "PLATFORM_DOMAIN"
#: Env var carrying the Railway API token.
ENV_RAILWAY_API_TOKEN = "RAILWAY_API_TOKEN"


def _pass() -> dict[str, Any]:
    # Fix 13: use ``status == "success"`` — the SAME value the create-app
    # success/failure contract uses everywhere else — not an off-schema third
    # value ("passed"). Callers (the CLI `--preflight` emit, agent callers)
    # branch on the `success|failed` pair; a stray "passed" broke that contract.
    return {"status": "success", "checkpoint": "preflight"}


def _fail(*, error_code: str, exit_code: int, message: str) -> dict[str, Any]:
    """Structured preflight failure — the orchestrator maps this onto a §R3
    ``FailureEnvelope`` via ``_fail(..., retryable=False)``."""
    return {
        "status": "failed",
        "checkpoint": "preflight",
        "error_code": error_code,
        "exit_code": exit_code,
        "error_message": message,
    }


def _render_fail(res: dict[str, Any]) -> dict[str, Any]:
    """Map a :mod:`agentic_core.bootstrap.render` failure onto a preflight
    failure. The Render ``error_code`` passes through unchanged."""
    code = res.get("error_code", "RENDER_CLI_ERROR")
    return _fail(
        error_code=code,
        exit_code=EXIT_AUTH if code == "RENDER_AUTH_FAILED" else EXIT_MISCONFIG,
        message=res.get("error_message", "Render CLI check failed"),
    )


def _auth_ok(
    argv: list[str], *, timeout: int
) -> tuple[bool, str]:
    """Run an auth-status command; return ``(ok, detail)``.

    A non-zero exit, a timeout, or a missing binary at exec time all count
    as "not authenticated" — the point of preflight is to refuse the run,
    not to distinguish transient CLI hiccups here.
    """
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"{argv[0]} auth check timed out after {timeout}s"
    except OSError as exc:  # binary vanished between which() and run()
        return False, f"{argv[0]} could not be executed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or f"{argv[0]} reported not authenticated"
    return True, ""


def run_preflight(
    *,
    deploy_target: Literal["railway", "render"] = "railway",
    with_deploy: bool = True,
    with_railway: bool = True,
    with_github: bool = True,
    with_logto: bool = True,
    keystore_path: Path | None = None,
    logto_endpoint: str | None = None,
    platform_domain: str | None = None,
    admin_app_id: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Validate every credential/CLI/token/keystore that this run needs.

    Returns ``{"status": "success", ...}`` if everything the run will use is
    present and usable, otherwise a structured failure dict (see module
    docstring for ``error_code`` values). No external resource is created
    and no secret is decrypted — this is pure validation.

    For the Render target with deploy enabled, the success dict also carries
    ``render_workspace_id`` and ``render_workspace_name`` (the active
    workspace the Blueprint is validated against).

    Args:
      deploy_target: ``"railway"`` (default, so existing callers keep the
        Railway checks) or ``"render"``.
      with_deploy: Render target only. When False, skip the Render checks.
        The Railway target ignores it and uses ``with_railway``.
      with_railway: Railway target only. When False (``--no-railway``), skip
        the railway binary, auth, and ``RAILWAY_API_TOKEN`` checks.
      with_github: When False (``--no-github``), skip the gh binary + auth
        checks.
      with_logto: When False (``--no-auth``), skip the Logto env + keystore
        checks.
      keystore_path: Explicit admin-keystore path. ``None`` ⇒ R7 precedence
        via :func:`keystore.resolve_keystore_path`.
      logto_endpoint: Explicit Logto endpoint. ``None`` ⇒ look for the
        ``LOGTO_ENDPOINT`` env var, else derive ``https://auth.<platform_domain>``
        (the SAME default the orchestrator uses at every use site). Preflight
        must not be *stricter* than the orchestrator: a run driven purely by
        ``--platform-domain`` (no ``LOGTO_ENDPOINT`` set) is valid and must not
        be rejected up front.
      platform_domain: Domain of the Logto API identifiers
        (``https://<slug>.<platform_domain>/api``). It also gives the derived
        endpoint when neither ``logto_endpoint`` nor ``LOGTO_ENDPOINT`` is set.
        ``None`` ⇒ the ``PLATFORM_DOMAIN`` env var. There is no built-in
        domain, so a Logto run without one fails here.
      admin_app_id: Explicit bootstrap admin M2M app id. ``None`` ⇒ look for
        the ``LOGTO_M2M_APP_ID`` env var.
      timeout: Per-subprocess auth-check timeout in seconds.
    """
    # --- GitHub: binary + auth ---------------------------------------
    if with_github:
        if shutil.which("gh") is None:
            return _fail(
                error_code="PREFLIGHT_GH_MISSING",
                exit_code=EXIT_MISCONFIG,
                message="`gh` binary not found on PATH (install the GitHub CLI).",
            )
        ok, detail = _auth_ok(["gh", "auth", "status"], timeout=timeout)
        if not ok:
            return _fail(
                error_code="PREFLIGHT_GH_AUTH_FAILED",
                exit_code=EXIT_AUTH,
                message=f"`gh auth status` failed — run `gh auth login`. {detail}".strip(),
            )

    # --- Render: binary + login + active workspace -------------------
    render_workspace: dict[str, Any] = {}
    if deploy_target == "render" and with_deploy:
        if shutil.which("render") is None:
            return _fail(
                error_code="RENDER_CLI_MISSING",
                exit_code=EXIT_MISCONFIG,
                message="`render` binary not found on PATH (install the Render CLI).",
            )
        who = render.whoami(timeout=timeout)
        if who.get("status") != "success":
            return _render_fail(who)
        ws = render.workspace_current(timeout=timeout)
        if ws.get("status") != "success":
            return _render_fail(ws)
        render_workspace = {
            "render_workspace_id": ws["workspace_id"],
            "render_workspace_name": ws["workspace_name"],
        }

    # --- Railway: binary + auth + token ------------------------------
    if deploy_target != "render" and with_railway:
        if shutil.which("railway") is None:
            return _fail(
                error_code="PREFLIGHT_RAILWAY_MISSING",
                exit_code=EXIT_MISCONFIG,
                message="`railway` binary not found on PATH (install the Railway CLI).",
            )
        ok, detail = _auth_ok(["railway", "whoami"], timeout=timeout)
        if not ok:
            return _fail(
                error_code="PREFLIGHT_RAILWAY_AUTH_FAILED",
                exit_code=EXIT_AUTH,
                message=f"`railway whoami` failed — run `railway login`. {detail}".strip(),
            )
        if not os.environ.get(ENV_RAILWAY_API_TOKEN):
            return _fail(
                error_code="PREFLIGHT_ENV_MISSING",
                exit_code=EXIT_MISCONFIG,
                message=f"{ENV_RAILWAY_API_TOKEN} is not set in the environment.",
            )

    # --- Logto: env + keystore ---------------------------------------
    if with_logto:
        # Admin app id: explicit param wins, else the env var must be set.
        resolved_admin_id = (
            admin_app_id
            if admin_app_id
            else os.environ.get(ENV_LOGTO_M2M_APP_ID)
        )
        if not resolved_admin_id:
            return _fail(
                error_code="PREFLIGHT_ENV_MISSING",
                exit_code=EXIT_MISCONFIG,
                message=f"{ENV_LOGTO_M2M_APP_ID} is not set (and no --admin-app-id given).",
            )
        # Logto API identifiers are https://<slug>.<platform_domain>/api.
        # There is no built-in domain: each operator names their own.
        if not ((platform_domain or "").strip() or os.environ.get(ENV_PLATFORM_DOMAIN, "").strip()):
            return _fail(
                error_code="PREFLIGHT_ENV_MISSING",
                exit_code=EXIT_MISCONFIG,
                message=(
                    "Logto sign-in needs a platform domain for its API "
                    "identifiers. Pass --platform-domain <domain>, or set "
                    f"{ENV_PLATFORM_DOMAIN} in .env."
                ),
            )
        # Logto endpoint: explicit param wins, else the env var, else the
        # derived default `https://auth.<platform_domain>` — the SAME resolution
        # every orchestrator use site performs. Preflight must never be
        # stricter than the run it gates, so a `--platform-domain`-only run
        # (no LOGTO_ENDPOINT set) is valid. The domain check above means that
        # resolution can never be empty, so there is no endpoint-missing branch.
        #
        # Admin keystore must exist and be readable — but we DO NOT decrypt.
        resolved_ks = (
            Path(keystore_path)
            if keystore_path
            else keystore.resolve_keystore_path(None)
        )
        if not resolved_ks.exists():
            return _fail(
                error_code="PREFLIGHT_KEYSTORE_MISSING",
                exit_code=EXIT_AUTH,
                message=f"admin keystore not found at {resolved_ks}.",
            )
        try:
            with resolved_ks.open("rb"):
                pass
        except OSError as exc:
            return _fail(
                error_code="PREFLIGHT_KEYSTORE_MISSING",
                exit_code=EXIT_AUTH,
                message=f"admin keystore at {resolved_ks} is not readable: {exc}",
            )

    return {**_pass(), **render_workspace}
