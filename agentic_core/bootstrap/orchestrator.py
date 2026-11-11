"""Bootstrap orchestrator — wires keystore → logto → github → railway → template.

This module is the single integration seam for ``agentic-core create-app``.
Each external concern lives in its own sibling module
(:mod:`agentic_core.bootstrap.keystore`,
:mod:`agentic_core.bootstrap.logto`,
:mod:`agentic_core.bootstrap.github`,
:mod:`agentic_core.bootstrap.railway`,
:mod:`agentic_core.bootstrap.template`,
:mod:`agentic_core.bootstrap.state`); the orchestrator owns sequencing,
checkpoint persistence, resume logic, and the success/failure envelopes
agent callers read as a JSON contract.

Spec invariants enforced here:

- **§R5 slug validation:** ``orchestrate_create_app`` validates the slug
  against ``^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$`` *before any external call*
  and raises ``SystemExit(1)`` on a mismatch. The accompanying test
  (``tests/unit/test_bootstrap_orchestrator.py::
  test_invalid_slug_exits_one_before_external_calls``) mocks every external
  module and asserts that none of them fire.

- **PERF-003 single decrypt:** ``keystore.decrypt_credential`` is called
  exactly once at orchestrator startup; the resulting ``bytearray`` is
  passed by reference to ``logto.provision_app``. The keystore wipes the
  buffer post-flight so the secret lives in process memory for the minimum
  possible window.

- **§R10 variant×invariant matrix:** ``_variant_to_logto_flags`` translates
  the ``variant`` string + ``with_auth`` opt-out into the four booleans
  ``provision_app`` takes. ``static-dashboard --no-auth`` short-circuits
  the entire Logto step (no decrypt, no provision call).

- **§R14 idempotency + resume:** ``resume=True`` reads the on-disk
  ``BootstrapState`` and skips every step whose checkpoint is ``True``.
  Each external module is already idempotent (Logto's ``provision_app``
  does find-then-create internally; ``gh repo create`` returns the existing
  URL on duplicate); the orchestrator just avoids re-issuing API calls
  unnecessarily by consulting the checkpoint.

- **§R3 failure envelope:** on terminal failure, the orchestrator writes
  the partial-state file (so a future ``--resume`` knows where to pick up),
  builds the §R3-shaped failure envelope, and either ``return``s it (callers
  that pass ``raise_on_failure=False``) or raises ``SystemExit(exit_code)``
  after stashing the envelope on the exception's ``args``.

- **Env hygiene:** Git commands receive ``env=_sanitized_env()`` — a copy of
  ``os.environ`` with ``AGENTIC_CORE_ADMIN_PASSPHRASE`` removed. Spec §R1
  forbids reading the passphrase from env in the first place, but this keeps
  it out of child processes even if a future caller violates that rule.

- **§R8 state file:** ``write_state`` is called after every successful
  step, ``delete_state`` after the full-success path completes. Partial
  failure leaves the state file in place for ``--resume``.

The public function is :func:`orchestrate_create_app`. It returns the
success envelope (R3 ``status: success`` shape) on success; on failure it
either returns a failure envelope (if ``raise_on_failure=False``) or raises
``SystemExit`` with the appropriate exit code.

The Typer CLI command lives in task #9 and is *not* in this module — it
imports :func:`orchestrate_create_app` and handles the JSON-vs-pretty
rendering. Keeping the orchestrator UI-free lets an agent caller call it
directly from Python in addition to spawning the subprocess.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Literal
from collections.abc import Callable

from agentic_core.bootstrap import (
    github,
    keystore,
    logto,
    preflight,
    railway,
    render,
    state,
    template,
    verify,
)
from agentic_core.bootstrap.state import BootstrapState


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: §R5 slug regex. Min 3, max 50 chars, lowercase alphanumeric + hyphen,
#: cannot start or end with hyphen.
SLUG_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")

#: Env var we deliberately strip from every subprocess invocation.
#: §R1 forbids reading the keystore passphrase from env in the first place;
#: this strip is belt-and-suspenders against any future caller that violates
#: that rule by setting it before invoking us.
FORBIDDEN_SUBPROCESS_ENV_VARS = ("AGENTIC_CORE_ADMIN_PASSPHRASE",)

#: Checkpoint marker names. The orchestrator writes one of these into the
#: state file's ``checkpoints`` dict after each successful step so ``--resume``
#: knows what to skip.
STEP_KEYSTORE = "keystore"
STEP_LOGTO = "logto"
STEP_GITHUB = "github"
STEP_GITHUB_SECRETS = "github_secrets"
STEP_RAILWAY = "railway"
STEP_RAILWAY_ENV = "railway_env"
STEP_POSTGRES = "postgres"
STEP_WIRING = "wiring"
STEP_TEMPLATE = "template"
STEP_GIT_COMMIT = "git_commit"
STEP_RAILWAY_DEPLOY = "railway_deploy"
STEP_VERIFY = "verify"
STEP_RENDER_VALIDATE = "render_validate"

STEP_LABELS = {
    STEP_KEYSTORE: "Admin keystore unlocked",
    STEP_LOGTO: "Logto resources provisioned",
    STEP_GITHUB: "GitHub repository ready",
    STEP_GITHUB_SECRETS: "GitHub Actions secrets configured",
    STEP_RAILWAY: "Railway project and service ready",
    STEP_RAILWAY_ENV: "Railway environment configured",
    STEP_POSTGRES: "Railway Postgres provisioned",
    STEP_WIRING: "Production domain, CORS, and redirect URIs configured",
    STEP_TEMPLATE: "Application template rendered",
    STEP_GIT_COMMIT: "Initial Git commit prepared",
    STEP_RAILWAY_DEPLOY: "Railway deployment succeeded",
    STEP_VERIFY: "Deployment verification passed",
    STEP_RENDER_VALIDATE: "Render Blueprint validated (no Render resources created)",
}

#: Description used when ``--description`` is omitted or blank. It matches the
#: ``app_description`` default in ``templates/mini-app/copier.yml``. The CLI
#: always passes a value to Copier, so without this the app got an empty
#: description (pyproject ``description = ""`` and an empty ``main.py`` title).
DEFAULT_APP_DESCRIPTION = "A mini-app"
#: Reserved domain (RFC 2606) for template values when no Logto domain is set.
PLACEHOLDER_PLATFORM_DOMAIN = "example.com"

#: Render target: settings the operator sets on the service before sign-in
#: works. Names only; the values never pass through agentic-core.
RENDER_AUTH_REQUIRED_SETTINGS = (
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "AUTH_ALLOWED_EMAILS",
)

#: Render target: what the generated app does not support yet.
RENDER_AUTH_LIMITATIONS = (
    "No mobile app: Google sign-in for the Expo app is not built yet.",
    "Agents (the app CLI and MCP server) need an agent key; see the app README.",
)

#: Render target: the auth block's next step. No Logto app is provisioned.
RENDER_AUTH_NEXT_STEP = (
    "Sign-in uses Google and is not configured yet; every protected API route "
    "returns 503 until it is. 1) Apply the Blueprint and leave the Google "
    "values empty. 2) Read the service URL in the Render dashboard. 3) In "
    "Google Cloud Console, create an OAuth client (Web application) with the "
    "redirect URI <service URL>/auth/callback. 4) Set GOOGLE_CLIENT_ID, "
    "GOOGLE_CLIENT_SECRET, and AUTH_ALLOWED_EMAILS on the service. Render "
    "generates SESSION_SECRET. See the app README."
)

#: Render target: the dashboard path that applies a Blueprint by hand.
RENDER_DASHBOARD_PATH = "New > Blueprint"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitized_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with forbidden vars stripped.

    Use this for every ``subprocess.run(env=...)`` invocation. The strip is
    belt-and-suspenders against R1 violations — see module docstring.
    """
    return {
        k: v for k, v in os.environ.items() if k not in FORBIDDEN_SUBPROCESS_ENV_VARS
    }


def _default_output_dir() -> Path:
    """Return the folder that holds the agentic-core checkout.

    New apps become siblings of the checkout, wherever it is cloned. This is
    the same folder that ``list-apps`` and ``teardown`` scan by default. Do not
    use a fixed home-directory path: other people clone the repo elsewhere.
    """
    return Path(__file__).resolve().parents[2].parent


def default_state_path(slug: str, output_dir: Path | None) -> Path:
    """Return ``<output_dir>/<slug>/.bootstrap-state.json``.

    ``output_dir=None`` uses the same default directory as
    :func:`orchestrate_create_app`. This function computes the path only. It
    creates no directory.
    """
    base = Path(output_dir) if output_dir is not None else _default_output_dir()
    return base / slug / ".bootstrap-state.json"


def recorded_bootstrap_state(
    slug: str, output_dir: Path | None
) -> BootstrapState | None:
    """Read the original options for a resume without changing state."""

    if not isinstance(slug, str) or not SLUG_REGEX.match(slug):
        return None
    try:
        return state.read_state(default_state_path(slug, output_dir))
    except (OSError, state.StateFileCorrupt):
        return None


def _resolve_logto_endpoint(logto_endpoint: str | None, platform_domain: str) -> str:
    """3-tier Logto endpoint resolution used at EVERY orchestrator site.

    Precedence: explicit ``--logto-endpoint`` arg → ``LOGTO_ENDPOINT`` env var →
    ``https://auth.<platform_domain>`` default. Unifying this (Fix #12) prevents
    drift where Logto resources get provisioned against one endpoint while the
    deployed app is told to auth against another because an operator set the env
    var and only some sites honored it.
    """
    return (
        logto_endpoint
        or os.environ.get("LOGTO_ENDPOINT")
        or f"https://auth.{platform_domain}"
    )


def _sanitize_logto_block(logto_result: dict | None) -> dict | None:
    """Return a copy of ``logto_result`` with secret-shaped keys removed.

    The M2M app secret (and any ``*_secret`` field) must never appear in the
    ``--json`` success envelope on stdout (Fix #4). Build a fresh dict — do NOT
    mutate ``logto_result`` itself, since later steps (verify's minter) still
    read the secret from the original.
    """
    if not isinstance(logto_result, dict):
        return logto_result
    return {k: v for k, v in logto_result.items() if not k.endswith("_secret")}


def _variant_to_logto_flags(
    variant: str, with_auth: bool
) -> tuple[bool, bool, bool, bool] | None:
    """Translate variant + with_auth into ``(with_spa, with_m2m, with_api, with_user)``.

    Returns ``None`` if Logto provisioning should be skipped entirely
    (``static-dashboard --no-auth``).

    §R10 matrix:

      ============================== === === === ===
      Variant                         SPA M2M API Usr
      ============================== === === === ===
      ``full``                         Y   Y   Y   Y
      ``lightweight-api``              N   Y   Y   Y
      ``static-dashboard --with-auth`` Y   N   N   Y
      ``static-dashboard --no-auth``   --- skip entirely
      ============================== === === === ===
    """
    if variant == "full":
        return (True, True, True, True)
    if variant == "lightweight-api":
        return (False, True, True, True)
    if variant == "static-dashboard":
        if not with_auth:
            return None
        return (True, False, False, True)
    raise ValueError(
        f"unknown variant {variant!r}; expected one of full | lightweight-api | static-dashboard"
    )


def _success_envelope(
    *,
    app_slug: str,
    variant: str,
    scaffolded_by: str,
    logto_result: dict | None,
    github_result: dict | None,
    railway_result: dict | None,
    railway_workspace: str | None,
    completed_steps: list[str],
    dry_run: bool = False,
    verify_summary: dict[str, Any] | None = None,
    deploy_target: Literal["railway", "render"] = "railway",
    render_block: dict[str, Any] | None = None,
    auth_provider: Literal["google", "logto"] = "logto",
    planned_steps: list[str] | None = None,
) -> dict[str, Any]:
    """Build the §A2/§R3 success envelope.

    Skipped sub-blocks are emitted as ``None`` (callers / JSON consumers
    treat absence vs null as equivalent; staying with explicit nulls keeps
    the shape stable across variants).

    The ``logto`` block is sanitized of secret-shaped keys (Fix #4) so the M2M
    app secret never lands in ``--json`` stdout. ``verify_summary`` (Fix #3),
    when present, records whether auth was actually re-checked this run so a
    resumed health-only run doesn't misrepresent itself as fully auth-verified.

    For Render, ``railway`` is ``None`` and ``render`` contains Blueprint details.
    The ``auth`` block lists any manual sign-in setup. The Render dry run
    includes the planned steps.
    """
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "status": "success",
        "app_slug": app_slug,
        "variant": variant,
        "scaffolded_by": scaffolded_by,
        "deploy_target": deploy_target,
        "completed_steps": completed_steps,
        "logto": _sanitize_logto_block(logto_result),
        "github": github_result,
        "railway": {
            "workspace": railway_workspace,
            **(railway_result or {}),
        }
        if railway_result
        else {"workspace": railway_workspace},
    }
    if deploy_target == "render":
        envelope["railway"] = None
        envelope["render"] = render_block
    if variant != "static-dashboard" and auth_provider == "google":
        envelope["auth"] = {
            "provider": "google_oidc",
            "status": "not_configured",
            "required_settings": list(RENDER_AUTH_REQUIRED_SETTINGS),
            "limitations": list(RENDER_AUTH_LIMITATIONS),
            "next_step": RENDER_AUTH_NEXT_STEP,
        }
        if deploy_target == "railway":
            envelope["auth"]["required_settings"] += ["SESSION_SECRET", "APP_BASE_URL"]
            envelope["auth"]["next_step"] = (
                "Set up Google sign-in in the Railway service. See the app README."
            )
    elif deploy_target == "render" and auth_provider == "logto" and variant != "static-dashboard":
        envelope["auth"] = {
            "provider": "logto",
            "status": "not_configured",
            "required_settings": ["LOGTO_ENDPOINT", "LOGTO_API_RESOURCE"],
            "limitations": ["No automated Logto provisioning on Render."],
            "next_step": (
                "Register a Logto application and set its service variables. See the app README."
            ),
        }
    if planned_steps is not None:
        envelope["planned_steps"] = planned_steps
    if verify_summary is not None:
        envelope["verify"] = verify_summary
    if dry_run:
        envelope["dry_run"] = True
    return envelope


def _failure_envelope(
    *,
    app_slug: str,
    checkpoint: str,
    exit_code: int,
    error_code: str,
    error_message: str,
    completed_steps: list[str],
    retryable: bool = True,
) -> dict[str, Any]:
    """Build the §R3 failure envelope.

    ``retryable`` (R8) defaults to True — transient failures resume cleanly.
    Structural failures (invalid slug/variant, corrupt state, a template that
    won't render into a working app) pass ``retryable=False`` so ``--resume``
    refuses to loop on a genuinely broken app.
    """
    return {
        "schema_version": 1,
        "status": "failed",
        "app_slug": app_slug,
        "checkpoint": checkpoint,
        "exit_code": exit_code,
        "error_code": error_code,
        "error_message": error_message,
        "completed_steps": completed_steps,
        "retryable": retryable,
    }


def _fail(
    *,
    app_slug: str,
    checkpoint: str,
    exit_code: int,
    error_code: str,
    error_message: str,
    completed_steps: list[str],
    state_path: Path | None,
    state_obj: BootstrapState | None,
    raise_on_failure: bool,
    retryable: bool = True,
) -> dict[str, Any]:
    """Persist state, build envelope, and either return or SystemExit.

    The state file is written (or left as-is, if we got here pre-decrypt)
    so a future ``--resume`` knows the checkpoint.

    ``retryable`` (R8) defaults True (backward compatible). Pass
    ``retryable=False`` for structural failures a ``--resume`` can never
    recover from (invalid slug/variant, corrupt state, an un-renderable
    template) so the resume driver reports rather than loops.
    """
    if state_obj is not None and state_path is not None:
        # Update the last_completed_step + persist whatever checkpoints we got.
        state_obj.last_completed_step = completed_steps[-1] if completed_steps else None
        # R8 resume gate: record the retryability of THIS failure so a future
        # --resume can refuse to re-run a structurally-broken app instead of
        # looping. Only IDs/bools/short codes are persisted — entropy-gate safe.
        state_obj.last_failure_retryable = retryable
        state_obj.last_failure_code = error_code
        try:
            state.write_state(state_path, state_obj)
        except state.StateFileEntropyError:
            # Fix #9: the R8 entropy gate ("a secret is about to hit disk") must
            # abort loudly — never be swallowed by best-effort persistence.
            raise
        except Exception:  # noqa: BLE001 — best-effort persistence
            pass

    envelope = _failure_envelope(
        app_slug=app_slug,
        checkpoint=checkpoint,
        exit_code=exit_code,
        error_code=error_code,
        error_message=error_message,
        completed_steps=completed_steps,
        retryable=retryable,
    )
    if raise_on_failure:
        # Pre-stash the envelope on the exception so callers wrapping us
        # in try/except SystemExit can recover it.
        exc = SystemExit(exit_code)
        exc.envelope = envelope  # type: ignore[attr-defined]
        raise exc
    return envelope


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def orchestrate_create_app(
    slug: str,
    *,
    variant: str = "full",
    description: str = "",
    with_auth: bool = True,
    with_db: bool = True,
    with_frontend: bool = True,
    with_mobile: bool = True,
    with_railway: bool = True,
    with_logto: bool = True,
    with_github: bool = True,
    railway_workspace: str | None = None,
    keystore_path: Path | None = None,
    passphrase_fd: int | None = None,
    resume: bool = False,
    dry_run: bool = False,
    scaffolded_by: Literal["agent", "manual"] = "manual",
    output_dir: Path | None = None,
    state_path: Path | None = None,
    template_dir: Path | None = None,
    raise_on_failure: bool = True,
    platform_domain: str | None = None,
    logto_endpoint: str | None = None,
    admin_app_id: str | None = None,
    github_owner: str | None = None,
    progress: Callable[[str], None] | None = None,
    deploy_target: Literal["railway", "render"] = "railway",
    auth_provider: Literal["google", "logto"] | None = None,
    with_deploy: bool = True,
) -> dict[str, Any]:
    """Scaffold + provision + deploy a new mini-app end-to-end.

    The function is UI-free: it returns a dict (the §R3 envelope) and lets
    the Typer command layer (task #9) handle JSON-vs-Rich rendering.

    Args:
      slug: Lowercase app slug. Validated against §R5 regex at function
        entry; invalid → ``SystemExit(1)`` before any external call.
      variant: ``full`` | ``lightweight-api`` | ``static-dashboard``.
      description: One-line description (passed to ``gh repo create``
        and the Copier ``app_description`` variable). Blank or omitted
        gives ``DEFAULT_APP_DESCRIPTION``.
      with_auth: Only meaningful for ``static-dashboard``: opt-in Logto SPA.
        For ``full`` and ``lightweight-api``, this is implicitly True.
      with_db: Skip Postgres provisioning if False (template-level).
      with_frontend: Render the web frontend for the ``full`` variant.
      with_mobile: Render the Expo mobile app for the ``full`` variant.
      with_railway: ``--no-railway`` opt-out for local dev only.
      with_logto: ``--no-auth`` opt-out — skips keystore decrypt + Logto.
        For ``static-dashboard --no-auth``, callers pass
        ``with_auth=False, with_logto=False`` together.
      with_github: ``--no-github`` opt-out for local dev only.
      railway_workspace: Forwarded to ``railway init -w <workspace>``;
        ``None`` ⇒ Railway CLI default (preserves the manual path).
      keystore_path: Override the keystore location. ``None`` ⇒ R7
        precedence (env var → VPS → local).
      passphrase_fd: ``--passphrase-fd N`` for an agent caller's pipe-based
        passphrase delivery. ``None`` ⇒ interactive ``getpass``.
      resume: Read the existing state file at ``state_path`` and skip
        already-done steps.
      dry_run: Short-circuit BEFORE any external call. Returns a synthetic
        success envelope describing what *would* happen.
      scaffolded_by: §R18 — ``agent`` or ``manual``. Persisted in state file
        and surfaced in the result envelope so agent callers + list-apps
        can distinguish.
      output_dir: Where Copier renders the template. ``None`` ⇒ a sibling
        directory next to the agentic-core repo.
      state_path: Where the ``.bootstrap-state.json`` lives. ``None`` ⇒
        ``<output_dir>/<slug>/.bootstrap-state.json``.
      template_dir: Copier source. ``None`` ⇒ the repo's
        ``templates/mini-app`` directory.
      raise_on_failure: True (default) ⇒ ``SystemExit(exit_code)`` on
        failure with the envelope stashed on the exception. False ⇒
        return the failure envelope dict.
      platform_domain: Domain for the Logto API resource URL
        (``https://<slug>.<platform_domain>/api``). ``None`` ⇒ the
        ``PLATFORM_DOMAIN`` env var. A Logto run without either fails
        preflight; other runs use ``PLACEHOLDER_PLATFORM_DOMAIN``.
      logto_endpoint: Logto base URL. ``None`` ⇒ ``LOGTO_ENDPOINT``, else
        ``https://auth.<platform_domain>``.
      admin_app_id: Bootstrap admin M2M app ID. ``None`` ⇒ read from
        ``LOGTO_M2M_APP_ID`` env var.
      github_owner: ``gh`` owner for repo creation. ``None`` (default) uses
        ``GITHUB_USER``, then the account ``gh`` is logged in as. If both are
        unknown, the repo name has no owner and ``gh`` uses the logged-in
        account.
      progress: Optional callback for human-readable progress messages.
      deploy_target: KTD12. ``railway`` (default, so direct Python callers
        keep the Railway pipeline) or ``render``. The Render target skips the
        keystore, Logto, and every Railway step whatever ``with_logto`` and
        ``with_railway`` say. Its order is preflight, template, Blueprint
        validation, then GitHub repo creation and push, so a failed
        validation leaves no GitHub repo behind (KTD2).
      with_deploy: Render target only. False skips the Render preflight
        checks and the Blueprint validation. ``render.yaml`` is still
        rendered. The Railway target ignores it and uses ``with_railway``.

    Returns:
      Success envelope (§R3 shape) on success, failure envelope on failure
      when ``raise_on_failure=False``.

    Raises:
      SystemExit: On any terminal failure when ``raise_on_failure=True``
        (default). The envelope is attached as ``.envelope`` on the
        exception for callers that wrap us.
    """
    # ------------------------------------------------------------------
    # Step 1: §R5 slug validation (no external calls yet)
    # ------------------------------------------------------------------
    # Compute the slug-prefixed env-var namespace once and reuse below.
    # The generated app's client.py + middleware consume vars under this
    # prefix (e.g. ``NOTES_M2M_APP_ID``) — see Invariant 1 in
    # ``docs/SECURITY.md`` and ``templates/.../cli/client.py.jinja``.
    # ``app_upper`` is only meaningful once we know ``slug`` is a string,
    # so defer the assignment until after the type guard below.
    if not isinstance(slug, str) or not SLUG_REGEX.match(slug):
        # Special-case: emit a quick failure envelope and exit. We don't
        # have a state file yet (slug is the directory key), so we can't
        # persist anything.
        envelope = _failure_envelope(
            app_slug=slug if isinstance(slug, str) else "",
            checkpoint="slug_validation",
            exit_code=1,
            error_code="INVALID_SLUG",
            error_message=(
                f"slug must match ^[a-z0-9][a-z0-9-]{{1,48}}[a-z0-9]$ "
                f"(got {slug!r})"
            ),
            completed_steps=[],
            retryable=False,
        )
        if raise_on_failure:
            exc = SystemExit(1)
            exc.envelope = envelope  # type: ignore[attr-defined]
            raise exc
        return envelope

    # Slug → env-var prefix (e.g. ``my-dashboard`` → ``MY_DASHBOARD``).
    # Generated apps consume Logto config under this namespace; see
    # ``templates/.../cli/client.py.jinja``'s ``_build_client``.
    app_upper = slug.replace("-", "_").upper()
    # TOML basic strings cannot contain literal newlines. Normalize all
    # whitespace at the orchestration boundary before this value reaches
    # GitHub metadata or the generated pyproject.toml. A blank value takes
    # the template default, so GitHub and the app get the same description.
    description = " ".join(description.split()) or DEFAULT_APP_DESCRIPTION

    # Render never provisions Logto. Google never needs the Logto admin
    # credentials, regardless of target.
    is_render = deploy_target == "render"
    # Normalized once: any value other than "render" takes the Railway path.
    target: Literal["railway", "render"] = "render" if is_render else "railway"
    if is_render:
        with_railway = False
    recorded_state = None
    if resume:
        resume_path = Path(state_path) if state_path is not None else default_state_path(
            slug, output_dir
        )
        try:
            recorded_state = state.read_state(resume_path)
        except (FileNotFoundError, state.StateFileCorrupt):
            pass  # The normal resume path reports these errors.
    provider = auth_provider or (
        recorded_state.resolved_auth_provider
        if recorded_state else ("google" if is_render else "logto")
    )
    with_logto = with_logto and not is_render and provider == "logto"
    # Logto API identifiers are https://<slug>.<platform_domain>/api. There is
    # no built-in domain: preflight fails a Logto run that names none. Other
    # runs render the template with a reserved placeholder domain.
    platform_domain = (platform_domain or "").strip() or os.environ.get(
        "PLATFORM_DOMAIN", ""
    ).strip() or None
    # A resume that names no domain reuses the one in the recorded API
    # resource (https://<slug>.<domain>/api), so a domain passed only as a
    # flag on the first run does not have to be typed again.
    if platform_domain is None and recorded_state is not None:
        recorded_resource = recorded_state.resource_ids.get("logto_api_resource", "")
        prefix, suffix = f"https://{slug}.", "/api"
        if recorded_resource.startswith(prefix) and recorded_resource.endswith(suffix):
            platform_domain = recorded_resource[len(prefix) : -len(suffix)] or None
    logto_domain_given = platform_domain is not None
    platform_domain = platform_domain or PLACEHOLDER_PLATFORM_DOMAIN

    # ------------------------------------------------------------------
    # Step 0: dry-run short-circuit (before ANY external call)
    # ------------------------------------------------------------------
    if dry_run:
        # A local check, no external call: a Logto plan with no domain would
        # fail preflight, so the preview must say so too.
        if with_logto and not logto_domain_given:
            return _fail(
                app_slug=slug,
                checkpoint="preflight",
                exit_code=5,
                error_code="PREFLIGHT_ENV_MISSING",
                error_message=(
                    "Logto sign-in needs a platform domain for its API "
                    "identifiers. Pass --platform-domain <domain>, or set "
                    "PLATFORM_DOMAIN in .env."
                ),
                completed_steps=[],
                state_path=None,
                state_obj=None,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
        planned: list[str] | None = None
        dry_render_block: dict[str, Any] | None = None
        if is_render:
            planned = ["preflight", STEP_TEMPLATE]
            if with_deploy:
                planned.append(STEP_RENDER_VALIDATE)
            if with_github:
                planned.extend([STEP_GITHUB, STEP_GIT_COMMIT])
            dry_render_block = {
                "workspace": None,
                "workspace_id": None,
                "blueprint_path": None,
                "validated": False,
                "status": "dry_run",
                "blueprint_url": None,
                "next_step": None,
            }
        return _success_envelope(
            app_slug=slug,
            variant=variant,
            scaffolded_by=scaffolded_by,
            logto_result=None,
            github_result=None,
            railway_result=None,
            railway_workspace=railway_workspace,
            completed_steps=[],
            dry_run=True,
            deploy_target=target,
            render_block=dry_render_block,
            auth_provider=provider,
            planned_steps=planned,
        )

    # ------------------------------------------------------------------
    # Reject an unknown variant before preflight for either target.
    # ------------------------------------------------------------------
    if variant not in template.VALID_VARIANTS:
        return _fail(
            app_slug=slug,
            checkpoint="variant_resolution",
            exit_code=1,
            error_code="INVALID_VARIANT",
            error_message=(
                f"unknown variant {variant!r}; expected one of "
                "full | lightweight-api | static-dashboard"
            ),
            completed_steps=[],
            state_path=None,
            state_obj=None,
            raise_on_failure=raise_on_failure,
            retryable=False,
        )

    # ------------------------------------------------------------------
    # Path defaulting. Compute the paths only: the directories are created
    # after preflight, so a failed preflight leaves nothing on disk.
    # ------------------------------------------------------------------
    if output_dir is None:
        # Sibling to the agentic-core checkout: <checkout parent>/<slug>
        output_dir = _default_output_dir()
    output_dir = Path(output_dir)
    # Railway links are directory-scoped. Provision from the target app
    # directory from the first Railway command onward so `railway init` never
    # links the agentic-core repo (or whichever directory invoked this CLI).
    app_dir = output_dir / slug

    if state_path is None:
        state_path = default_state_path(slug, output_dir)
    state_path = Path(state_path)

    # ------------------------------------------------------------------
    # KTD8: a resume must keep the target the run started with. The
    # checkpoints of one target mean nothing to the other. Check this BEFORE
    # preflight, which checks only the requested target's CLIs and
    # credentials and would otherwise hide this error behind an unrelated
    # one. Pass no state object so the refusal does not rewrite the state
    # file: a later resume with the original target must still work.
    # ------------------------------------------------------------------
    if recorded_state is not None:
        if recorded_state.deploy_target != target:
            return _fail(
                app_slug=slug,
                checkpoint="resume",
                exit_code=1,
                error_code="DEPLOY_TARGET_MISMATCH",
                error_message=(
                    f"--resume refused: the state file at {state_path} is "
                    f"for deploy target '{recorded_state.deploy_target}', but this "
                    f"run asked for '{target}'. Resume with "
                    f"--deploy-target {recorded_state.deploy_target}, or delete "
                    "the state file and start a fresh run."
                ),
                completed_steps=[
                    k for k, v in recorded_state.checkpoints.items() if v
                ],
                state_path=None,
                state_obj=None,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )

        if recorded_state.resolved_auth_provider != provider:
            return _fail(
                app_slug=slug,
                checkpoint="resume",
                exit_code=1,
                error_code="AUTH_PROVIDER_MISMATCH",
                error_message=(
                    f"--resume refused: this run started with "
                    f"--auth-provider {recorded_state.resolved_auth_provider}."
                ),
                completed_steps=[k for k, v in recorded_state.checkpoints.items() if v],
                state_path=None,
                state_obj=None,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )

    # ------------------------------------------------------------------
    # Preflight: validate every credential/CLI/token/keystore this run needs
    # BEFORE any external system is touched (§R6 / SC4). A failure here is
    # structural — nothing has been created — so it exits non-retryable.
    # dry-run already returned above (no live creds needed for a plan preview).
    # ------------------------------------------------------------------
    pf = preflight.run_preflight(
        deploy_target=target,
        with_deploy=with_deploy,
        with_railway=with_railway,
        with_github=with_github,
        with_logto=with_logto,
        keystore_path=keystore_path,
        logto_endpoint=logto_endpoint,
        platform_domain=platform_domain if logto_domain_given else None,
        admin_app_id=admin_app_id,
    )
    if pf.get("status") == "failed":
        return _fail(
            app_slug=slug,
            checkpoint="preflight",
            exit_code=pf.get("exit_code", 5),
            error_code=pf.get("error_code", "PREFLIGHT_FAILED"),
            error_message=pf.get("error_message", "preflight validation failed"),
            completed_steps=[],
            # No state file yet — nothing was created, so we can't (and
            # needn't) persist a checkpoint.
            state_path=None,
            state_obj=None,
            raise_on_failure=raise_on_failure,
            retryable=False,
        )

    # Resolve the GitHub owner after preflight (which checked `gh auth`). Never
    # assume the maintainer's account: other people run this CLI.
    if github_owner is None:
        github_owner = os.environ.get("GITHUB_USER", "").strip()
        if not github_owner and with_github:
            github_owner = github.logged_in_login() or ""

    def _repo_full_name() -> str:
        """``owner/slug`` for gh, from the created repo's URL when known."""
        created = github.repo_from_url(state_obj.resource_ids.get("github_repo_url", ""))
        if created:
            return created
        return f"{github_owner}/{slug}" if github_owner else slug

    def _repo_url() -> str:
        return state_obj.resource_ids.get("github_repo_url") or (
            f"https://github.com/{_repo_full_name()}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    app_dir.mkdir(parents=True, exist_ok=True)

    if template_dir is None:
        # Find the repo-local templates/mini-app directory.
        template_dir = Path(__file__).resolve().parents[2] / "templates" / "mini-app"
    template_dir = Path(template_dir)

    # ------------------------------------------------------------------
    # Load (or create) the bootstrap state
    # ------------------------------------------------------------------
    state_obj: BootstrapState
    completed_steps: list[str] = []
    if resume and state_path.exists():
        try:
            state_obj = state.read_state(state_path)
            completed_steps = [k for k, v in state_obj.checkpoints.items() if v]
            # The KTD8 deploy-target check ran before preflight.
            # R8 resume gate: if the previous run ended in a STRUCTURAL
            # (non-retryable) failure, --resume must NOT re-attempt — a template
            # bug / broken app config / silent-no-op PATCH can never be fixed by
            # re-running the same steps, so a naive resume would either loop or
            # (worse) declare a broken app done. Refuse loudly and point the
            # operator at the underlying failure instead. (Retryable failures —
            # 429s, slow rollouts, transient CLI blips — leave the marker True/
            # None and resume proceeds normally.)
            if state_obj.last_failure_retryable is False:
                failed_code = state_obj.last_failure_code or "NON_RETRYABLE_FAILURE"
                return _fail(
                    app_slug=slug,
                    checkpoint="resume",
                    exit_code=3,
                    error_code=failed_code,
                    error_message=(
                        f"--resume refused: the previous run failed with a "
                        f"non-retryable (structural) error ({failed_code}) that "
                        f"re-running cannot fix. Repair the app/template and start "
                        f"a fresh run (delete {state_path} or omit --resume) instead."
                    ),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                    retryable=False,
                )
        except state.StateFileCorrupt as exc:
            return _fail(
                app_slug=slug,
                checkpoint="resume",
                exit_code=1,
                error_code="STATE_FILE_CORRUPT",
                error_message=str(exc),
                completed_steps=[],
                state_path=None,
                state_obj=None,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
    else:
        state_obj = BootstrapState(
            app_slug=slug,
            variant=variant,
            scaffolded_by=scaffolded_by,
            created_at=datetime.now(tz=UTC),
            checkpoints={},
            resource_ids={},
            last_completed_step=None,
            deploy_target=target,
            auth_provider=provider,
        )

    def _mark(step: str) -> None:
        state_obj.checkpoints[step] = True
        state_obj.last_completed_step = step
        completed_steps.append(step)
        # Persist after every successful step so a crash mid-flight is
        # recoverable.
        try:
            state.write_state(state_path, state_obj)
        except state.StateFileEntropyError:
            # Fix #9: never swallow the entropy gate — a secret about to hit disk
            # must abort loudly, not be dropped and continue.
            raise
        except Exception:  # noqa: BLE001
            pass
        if progress is not None:
            progress(f"[green]✓[/green] {STEP_LABELS.get(step, step)}")

    def _progress(message: str) -> None:
        if progress is not None:
            progress(message)

    def _done(step: str) -> bool:
        return state_obj.checkpoints.get(step, False)

    # ------------------------------------------------------------------
    # A resume must keep the Logto domain the run started with. The API
    # resource in state was created for that domain; another domain would
    # create a second resource, or render config that names the wrong one.
    # Pass no state object so the refusal does not rewrite the state file.
    # ------------------------------------------------------------------
    recorded_resource = state_obj.resource_ids.get("logto_api_resource")
    if with_logto and recorded_resource:
        expected_resource = logto.api_resource_indicator(slug, platform_domain)
        if recorded_resource != expected_resource:
            return _fail(
                app_slug=slug,
                checkpoint="resume",
                exit_code=1,
                error_code="PLATFORM_DOMAIN_MISMATCH",
                error_message=(
                    f"--resume refused: this app's Logto API resource is "
                    f"{recorded_resource}, but this run's platform domain "
                    f"gives {expected_resource}. Resume with the original "
                    "--platform-domain."
                ),
                completed_steps=completed_steps,
                state_path=None,
                state_obj=None,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )

    # ------------------------------------------------------------------
    # Decide what Logto provisioning is needed (§R10 matrix)
    # ------------------------------------------------------------------
    logto_flags: tuple[bool, bool, bool, bool] | None = None
    if with_logto:
        try:
            logto_flags = _variant_to_logto_flags(variant, with_auth=with_auth)
        except ValueError as exc:
            return _fail(
                app_slug=slug,
                checkpoint="variant_resolution",
                exit_code=1,
                error_code="INVALID_VARIANT",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
        # `static-dashboard --no-auth` → logto_flags is None → skip Logto entirely
        if logto_flags is None:
            with_logto = False

    # Finding 5: unpack the four R10 booleans on a path reached by BOTH fresh
    # and resume runs (not just inside the STEP_LOGTO block below). The
    # secret-dependent guards in STEP_GITHUB_SECRETS / STEP_RAILWAY_ENV must know
    # whether this variant even HAS an M2M secret: `static-dashboard --with-auth`
    # → with_m2m=False → a None `logto_result` on resume means "no secret exists"
    # (a no-op), NOT "unrecoverable secret". Defaults to all-False when Logto is
    # skipped entirely so no NameError downstream.
    with_spa, with_m2m, with_api_resource, with_user = (
        logto_flags if logto_flags else (False, False, False, False)
    )

    # ------------------------------------------------------------------
    # Step 2 + 3: Decrypt keystore + provision Logto (only if needed)
    # ------------------------------------------------------------------
    logto_result: dict | None = None
    admin_credential: bytearray | None = None
    if with_logto and not _done(STEP_LOGTO):
        # Step 2: Decrypt keystore (called exactly once per orchestration — PERF-003)
        try:
            resolved_path = (
                Path(keystore_path)
                if keystore_path
                else keystore.resolve_keystore_path(None)
            )
            envelope_dict = keystore.load_keystore(resolved_path)
            passphrase = keystore.read_passphrase(passphrase_fd)
            admin_credential = keystore.decrypt_credential(envelope_dict, passphrase)
            _mark(STEP_KEYSTORE)
        except keystore.KeystoreNotFound as exc:
            return _fail(
                app_slug=slug,
                checkpoint=STEP_KEYSTORE,
                exit_code=2,
                error_code="KEYSTORE_NOT_FOUND",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        except keystore.KeystoreDecryptionError as exc:
            return _fail(
                app_slug=slug,
                checkpoint=STEP_KEYSTORE,
                exit_code=2,
                error_code="KEYSTORE_DECRYPT_FAILED",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        except keystore.PassphraseReadError as exc:
            return _fail(
                app_slug=slug,
                checkpoint=STEP_KEYSTORE,
                exit_code=2,
                error_code="PASSPHRASE_READ_FAILED",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )

        # Step 3: provision Logto with the four R10 booleans (already unpacked
        # above into with_spa / with_m2m / with_api_resource / with_user).
        try:
            resolved_endpoint = _resolve_logto_endpoint(logto_endpoint, platform_domain)
            # Default to env var if no explicit admin_app_id was passed. An
            # empty string is still forwarded — `provision_app` accepts it
            # because the LogtoManagementClient only consults it when minting
            # a management token, and tests mock provision_app so the empty
            # admin_app_id never reaches the live HTTP path.
            resolved_admin_id = (
                admin_app_id
                if admin_app_id is not None
                else os.environ.get("LOGTO_M2M_APP_ID", "")
            )
            # Incremental persistence (§R2 / KTD3): write each Logto resource
            # ID into the state file the moment provision_app creates (or, on
            # resume, adopts via its finder) that resource — not in one batch
            # after the whole call returns. A mid-sequence failure therefore
            # leaves the already-created SPA/M2M recorded, so --resume can
            # adopt them instead of orphaning them. Mirrors _mark's
            # write-after-step but at sub-step granularity. The callback only
            # ever receives non-secret IDs/URLs (the M2M secret is never
            # passed) so the R8 entropy gate holds.
            def _persist_logto_id(key: str, value: str) -> None:
                state_obj.resource_ids[key] = value
                try:
                    state.write_state(state_path, state_obj)
                except state.StateFileEntropyError:
                    # Fix #9: entropy gate must surface, not be swallowed.
                    raise
                except Exception:  # noqa: BLE001 — best-effort persistence
                    pass

            logto_result = logto.provision_app(
                slug=slug,
                endpoint=resolved_endpoint,
                admin_app_id=resolved_admin_id,
                admin_credential=admin_credential,
                platform_domain=platform_domain,
                with_spa=with_spa,
                with_m2m=with_m2m,
                with_api_resource=with_api_resource,
                with_user=with_user,
                persist=_persist_logto_id,
            )
            _mark(STEP_LOGTO)
        except logto.RateLimitError as exc:
            return _fail(
                app_slug=slug,
                checkpoint=STEP_LOGTO,
                exit_code=6,
                error_code="LOGTO_429_RATE_LIMITED",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        except logto.LogtoAuthError as exc:
            return _fail(
                app_slug=slug,
                checkpoint=STEP_LOGTO,
                exit_code=2,
                error_code="LOGTO_AUTH_FAILED",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        except logto.LogtoSecretUnrecoverable as exc:
            # Wave-1 type: the M2M secret couldn't be recovered from Logto. Like
            # the other unrecoverable-secret paths this is NON-retryable — a
            # re-run can never recover it, so --resume must refuse rather than
            # loop. Caught BEFORE the generic LogtoError (retryable) handler.
            return _fail(
                app_slug=slug,
                checkpoint=STEP_LOGTO,
                exit_code=3,
                error_code="LOGTO_SECRET_UNRECOVERABLE",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
        except logto.LogtoError as exc:
            return _fail(
                app_slug=slug,
                checkpoint=STEP_LOGTO,
                exit_code=3,
                error_code="LOGTO_API_FAILED",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        except state.StateFileEntropyError:
            # Fix #9: an entropy-gate abort raised by the persist callback (or the
            # STEP_LOGTO mark) must propagate — NOT be masked as LOGTO_UNEXPECTED
            # by the catch-all below. A secret about to hit disk aborts loudly.
            raise
        except Exception as exc:  # noqa: BLE001
            return _fail(
                app_slug=slug,
                checkpoint=STEP_LOGTO,
                exit_code=3,
                error_code="LOGTO_UNEXPECTED",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        finally:
            # Zero the credential as soon as Logto is done with it.
            if admin_credential is not None:
                admin_credential[:] = b"\x00" * len(admin_credential)
                admin_credential = None

    # ------------------------------------------------------------------
    # Step 4: Create GitHub repo
    # ------------------------------------------------------------------
    # The Railway target creates the repo here. The Render target creates it
    # after the Blueprint validation step (KTD2), so the same helper runs there.
    github_result: dict | None = None

    def _create_github_repo_step() -> dict[str, Any] | None:
        """Run Step 4. Return a failure envelope, or ``None`` to continue."""
        nonlocal github_result
        if not with_github or _done(STEP_GITHUB):
            return None
        repo_name = _repo_full_name()
        gh_res = github.create_github_repo(name=repo_name, description=description)
        github_result = gh_res
        if gh_res.get("status") == "failed":
            return _fail(
                app_slug=slug,
                checkpoint=STEP_GITHUB,
                exit_code=3,
                error_code=gh_res.get("error_code", "GH_CLI_ERROR"),
                error_message=gh_res.get("error_message", "unknown gh failure"),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        state_obj.resource_ids["github_repo_url"] = gh_res.get("url", "")
        _mark(STEP_GITHUB)
        return None

    if not is_render:
        github_failure = _create_github_repo_step()
        if github_failure is not None:
            return github_failure

    # ------------------------------------------------------------------
    # Step 5: Set GitHub secrets (only if Logto provisioned + github created)
    # ------------------------------------------------------------------
    if with_github and with_logto and not _done(STEP_GITHUB_SECRETS):
        # Fix 1 (P0): the M2M app secret only exists in memory during the
        # ORIGINAL STEP_LOGTO run (`logto_result`); it is deliberately never
        # persisted to state (R8 entropy gate). On a --resume that jumped past
        # a checkpointed STEP_LOGTO, `logto_result is None` — the secret is
        # gone. Silently skipping this step (the old behaviour) shipped an app
        # whose GitHub Actions secret was never pushed, then reported success.
        # Fail loud, non-retryable: resume can NEVER recover the secret, so the
        # operator must delete the state file and re-run from scratch.
        #
        # Finding 5: gate this on `with_m2m`. A variant WITHOUT an M2M app
        # (`static-dashboard --with-auth` → with_m2m=False) has NO secret to
        # push — a None `logto_result` on resume is expected, not unrecoverable.
        # Only a variant that genuinely HAS a secret is wedged when it's gone.
        if with_m2m and logto_result is None:
            return _fail(
                app_slug=slug,
                checkpoint=STEP_GITHUB_SECRETS,
                exit_code=3,
                error_code="LOGTO_SECRET_UNRECOVERABLE",
                error_message=(
                    "cannot push the M2M app secret to GitHub Actions on "
                    "--resume: it is only available in memory during the "
                    "original STEP_LOGTO run and is never persisted (R8 entropy "
                    "gate), so resume cannot recover it. Delete the state file "
                    f"({state_path}) and re-run from scratch."
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
        if not with_m2m:
            # No M2M app for this variant ⇒ no secret to push ⇒ step is a no-op.
            # (Do NOT touch logto_result — it may legitimately be None here.)
            _mark(STEP_GITHUB_SECRETS)
        else:
            repo_name = _repo_full_name()
            # Push the M2M secret as a GitHub Actions secret for the per-app CI,
            # under the slug-prefixed namespace the generated app's CLI consumes
            # (e.g. ``NOTES_M2M_APP_SECRET``). Only secret values go into GH
            # Actions secrets; non-secret IDs (resource URL, app id, user sub)
            # are written to Railway env vars in STEP_RAILWAY_ENV below.
            # (logto_result is guaranteed non-None here: the guard above fired
            # for with_m2m + None.)
            m2m_secret = logto_result.get("m2m_app_secret")
            if m2m_secret:
                secret_res = github.set_repo_secret(
                    repo=repo_name,
                    name=f"{app_upper}_M2M_APP_SECRET",
                    value=str(m2m_secret),
                )
                if secret_res.get("status") == "failed":
                    return _fail(
                        app_slug=slug,
                        checkpoint=STEP_GITHUB_SECRETS,
                        exit_code=3,
                        error_code=secret_res.get("error_code", "GH_CLI_ERROR"),
                        error_message=secret_res.get(
                            "error_message", "gh secret set failed"
                        ),
                        completed_steps=completed_steps,
                        state_path=state_path,
                        state_obj=state_obj,
                        raise_on_failure=raise_on_failure,
                    )
            _mark(STEP_GITHUB_SECRETS)

    # ------------------------------------------------------------------
    # Step 6: Create Railway project
    # ------------------------------------------------------------------
    railway_result: dict | None = None
    if with_railway and not _done(STEP_RAILWAY):
        # check(): if a Railway project is already linked (e.g. a prior run
        # ran `railway init` but crashed before marking the checkpoint), ADOPT
        # it instead of re-running init — which is not idempotent and errors on
        # re-run (R3 / SC2). `get_linked_project` is non-fatal: it degrades to
        # "not linked" on any ambiguity, so we fall through to init.
        linked = railway.get_linked_project(cwd=str(app_dir))
        if linked.get("linked"):
            # Finding 1 (P0): only ADOPT when the linked project's NAME matches
            # the app slug. If the cwd is linked to a DIFFERENT project (a prior
            # app's dir, a stale link, the wrong cwd), adopting it on faith would
            # silently provision env vars / Postgres / deploys against the WRONG
            # project. A None name means the link can't be verified — treat that
            # as a non-match too (an unverifiable adopt is as dangerous as a
            # wrong one). Fail loud + non-retryable: the operator must unlink or
            # run from the app directory (a loud stop is recoverable; a silent
            # wrong-project provision is not).
            linked_name = linked.get("name")
            if linked_name != slug:
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_RAILWAY,
                    exit_code=3,
                    error_code="RAILWAY_PROJECT_MISMATCH",
                    error_message=(
                        "refusing to adopt the currently-linked Railway "
                        f"project: this directory is linked to project "
                        f"'{linked_name if linked_name is not None else '<unknown>'}' "
                        f"but this run expected '{slug}'. Provisioning against "
                        "the wrong project would leak env vars / Postgres / a "
                        "deploy into it. Unlink it (`railway unlink`) or run the "
                        f"pipeline from the '{slug}' app directory, then retry."
                    ),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                    retryable=False,
                )
            railway_result = {
                "status": "success",
                "name": slug,
                "workspace": railway_workspace,
                "project_id": linked.get("project_id"),
                "service_id": linked.get("service_id"),
                "adopted": True,
            }
            project_id = linked.get("project_id")
            if project_id:
                # Persist IDs only (never secrets) — a UUID is entropy-gate
                # safe (hyphens segment it below the 32-char threshold).
                state_obj.resource_ids["railway_project_id"] = str(project_id)
        else:
            rw_res = railway.create_railway_project(
                name=slug, workspace=railway_workspace, cwd=str(app_dir)
            )
            railway_result = rw_res
            if rw_res.get("status") == "failed":
                # Transient Railway failures stay retryable (default) so
                # --resume can re-attempt; only structural failures opt out.
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_RAILWAY,
                    exit_code=3,
                    error_code=rw_res.get("error_code", "RAILWAY_CLI_ERROR"),
                    error_message=rw_res.get(
                        "error_message", "railway init failed"
                    ),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                )
            project_id = rw_res.get("project_id")
            if project_id:
                state_obj.resource_ids["railway_project_id"] = str(project_id)

        # `railway init` creates an EMPTY project with NO service — the next
        # `railway variables --set` would fail "Project has no services". Create
        # a service (named for the slug, same as the project) so env-set /
        # domain / deploy have a target. Idempotent: an already-present service
        # (adopt path / resume) is a tolerated adopt, so it is safe to call on
        # BOTH the fresh-init and adopt branches. A failure is transient →
        # retryable so --resume re-attempts.
        svc_res = railway.create_railway_service(service=slug, cwd=str(app_dir))
        if svc_res.get("status") == "failed":
            return _fail(
                app_slug=slug,
                checkpoint=STEP_RAILWAY,
                exit_code=3,
                error_code=svc_res.get("error_code", "RAILWAY_CLI_ERROR"),
                error_message=svc_res.get(
                    "error_message", "railway add --service failed"
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        service_id = svc_res.get("service_id")
        if service_id:
            # IDs only (never secrets) — entropy-gate safe.
            state_obj.resource_ids["railway_service_id"] = str(service_id)
        _mark(STEP_RAILWAY)

    # ------------------------------------------------------------------
    # Step 7: Set Railway env vars
    # ------------------------------------------------------------------
    if with_railway and with_logto and not _done(STEP_RAILWAY_ENV):
        # Fix 1 (P0): same unrecoverable-secret hazard as STEP_GITHUB_SECRETS.
        # The M2M secret lives only in `logto_result` from the original
        # STEP_LOGTO run and is never persisted. On a --resume past a
        # checkpointed STEP_LOGTO it is None, so we cannot push the Railway env
        # vars (incl. `<APP>_M2M_APP_SECRET`). Silently skipping shipped an app
        # missing its auth env; fail loud, non-retryable instead.
        #
        # Finding 5: gate the unrecoverable-secret failure on `with_m2m`. Only a
        # variant that HAS an M2M secret is wedged when `logto_result` is None on
        # resume. The NON-secret vars (endpoint, IDs) are widened to source from
        # persisted state below, so a secret-less variant (static-dashboard) can
        # resume and complete.
        if with_m2m and logto_result is None:
            return _fail(
                app_slug=slug,
                checkpoint=STEP_RAILWAY_ENV,
                exit_code=3,
                error_code="LOGTO_SECRET_UNRECOVERABLE",
                error_message=(
                    "cannot push the M2M app secret to Railway env vars on "
                    "--resume: it is only available in memory during the "
                    "original STEP_LOGTO run and is never persisted (R8 entropy "
                    "gate), so resume cannot recover it. Delete the state file "
                    f"({state_path}) and re-run from scratch."
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
        # Push per-app Logto vars to Railway so the deployed app can auth.
        # All names are slug-prefixed (Invariant 1) so the generated app's
        # client.py + middleware can locate them without colliding with the
        # admin-side ``LOGTO_*`` namespace used by ``agentic-core`` itself.
        #
        # Finding 5: source each NON-secret value from `logto_result` on a fresh
        # run, else from the incrementally-persisted `state_obj.resource_ids` on
        # resume (the four IDs were persisted by provision_app's finder; the
        # endpoint is re-derivable). This is what lets a secret-less variant
        # resume through this step. The M2M SECRET is only ever in memory
        # (never persisted), so it is included ONLY when with_m2m AND it's
        # actually present (fresh run) — skipped otherwise.
        lr = logto_result or {}
        rids = state_obj.resource_ids
        resolved_endpoint = _resolve_logto_endpoint(logto_endpoint, platform_domain)
        endpoint_val = lr.get("endpoint") or resolved_endpoint
        m2m_id_val = lr.get("m2m_app_id") or rids.get("logto_m2m_app_id", "")
        api_resource_val = lr.get("api_resource") or rids.get("logto_api_resource", "")
        spa_id_val = lr.get("spa_client_id") or rids.get("logto_spa_client_id", "")
        # The actor identity must be the Logto user *id*, because that is what
        # a browser login puts in the token's `sub`. `user_sub`/`logto_user_sub`
        # hold the USERNAME (teardown resolves the user by username), so using
        # them here would make the same human land on two different
        # UserProfile rows — one from the SPA, one from the CLI. Fall back to
        # the username only when no id is recorded (pre-existing state files).
        user_id_val = (
            lr.get("user_id")
            or rids.get("logto_user_id", "")
            or lr.get("user_sub")
            or rids.get("logto_user_sub", "")
        )
        env_pairs = [
            # Slug-prefixed CLI namespace (the generated app's cli/client.py reads
            # these — see `_build_client`). DO NOT remove them.
            (f"{app_upper}_LOGTO_ENDPOINT", endpoint_val),
            (f"{app_upper}_M2M_APP_ID", m2m_id_val),
            (f"{app_upper}_API_RESOURCE", api_resource_val),
            (f"{app_upper}_SPA_CLIENT_ID", spa_id_val),
            (f"{app_upper}_USER_SUB", user_id_val),
            # Seed the actor-sub allowlist with this app's own user. The
            # middleware fails closed on an empty allowlist (correctly), so
            # without this every M2M call 403s on a freshly bootstrapped app.
            (f"{app_upper}_M2M_ACTOR_SUBS", user_id_val),
            # Fix #2: UNPREFIXED backend namespace. The template's
            # Settings(BaseSettings) (config.py.jinja) has NO env_prefix, so it
            # reads by field name; the JWT middleware fails closed with HTTP 503
            # if LOGTO_M2M_AGENT_APP_ID is empty. Without these the deployed
            # backend has no Logto config and 503s on every request.
            ("LOGTO_ENDPOINT", endpoint_val),
            ("LOGTO_API_RESOURCE", api_resource_val),
            ("LOGTO_M2M_AGENT_APP_ID", m2m_id_val),
            ("LOGTO_APP_ID", spa_id_val),
        ]
        # Frontend BUILD-TIME vars. Vite bakes import.meta.env.VITE_* into the
        # JS bundle at BUILD time (config.ts throws if VITE_LOGTO_ENDPOINT /
        # VITE_LOGTO_APP_ID are missing). Railway passes service variables to the
        # Docker build as build args (the Dockerfile declares matching ARGs), so
        # these must be set on the service BEFORE the deploy build. Only the
        # "full" variant renders a frontend (include_frontend). Without these the
        # frontend builds with empty Logto config and the SPA errors on load
        # (live-surfaced: blank page in the browser though /health is 200).
        if variant == "full":
            env_pairs.extend(
                [
                    ("VITE_LOGTO_ENDPOINT", endpoint_val),
                    ("VITE_LOGTO_APP_ID", spa_id_val),
                    ("VITE_LOGTO_API_RESOURCE", api_resource_val),
                ]
            )
        if with_m2m:
            m2m_secret_val = lr.get("m2m_app_secret", "")
            if m2m_secret_val:
                # Prefixed (CLI) + unprefixed (backend middleware) secret. Only on
                # a fresh run — on resume the secret is gone (never persisted, R8
                # entropy gate), so the backend M2M-agent auth can't be
                # reconfigured mid-resume (known secret-unrecoverable limitation).
                env_pairs.append((f"{app_upper}_M2M_APP_SECRET", m2m_secret_val))
                env_pairs.append(("LOGTO_M2M_AGENT_APP_SECRET", m2m_secret_val))
        for name, value in env_pairs:
            if not value:
                continue
            # Target the app's service (=slug); `railway variables` requires -s
            # once the project has multiple services (app + Postgres).
            env_res = railway.set_env_var(
                name=name, value=str(value), service=slug, cwd=str(app_dir)
            )
            if env_res.get("status") == "failed":
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_RAILWAY_ENV,
                    exit_code=3,
                    error_code=env_res.get("error_code", "RAILWAY_CLI_ERROR"),
                    error_message=env_res.get(
                        "error_message", "railway variables --set failed"
                    ),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                )
        _mark(STEP_RAILWAY_ENV)

    # ------------------------------------------------------------------
    # Step 7.5: Provision Postgres (R4 / SC3)
    # ------------------------------------------------------------------
    # The orchestrator owns runtime wiring: it provisions Postgres so the
    # template's ``preDeployCommand: alembic upgrade head`` has a reachable DB
    # on first deploy (the Python rewrite dropped this, so apps crashed on
    # first request). Gated on ``with_db``; ``with_railway`` is implied because
    # Postgres is a Railway service attached to the linked project.
    if with_railway and with_db and not _done(STEP_POSTGRES):
        # check(): a resume must ADOPT an already-provisioned DB rather than
        # provision a second one (KTD4 / Risk R-c). ``has_database`` is
        # non-fatal — it degrades to "no DB" on any ambiguity, so we fall
        # through to ``add_database``.
        existing_db = railway.has_database(cwd=str(app_dir))
        if not existing_db.get("has_database"):
            db_res = railway.add_database(kind="postgres", cwd=str(app_dir))
            if db_res.get("status") == "failed":
                # A failed ``railway add`` is transient (auth blip, CLI/network
                # error) — stay retryable (default) so --resume re-attempts.
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_POSTGRES,
                    exit_code=3,
                    error_code=db_res.get("error_code", "RAILWAY_CLI_ERROR"),
                    error_message=db_res.get(
                        "error_message", "railway add --database postgres failed"
                    ),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                )
        # Link the DB to the APP service. Railway does NOT auto-inject
        # DATABASE_URL into the app service — the connection string lives on the
        # "Postgres" service, and the app service only receives it via an
        # explicit reference variable. Without this the app + the alembic
        # preDeployCommand boot with no DATABASE_URL and the container crashes on
        # startup (live-surfaced). The reference token itself is not a secret
        # (Railway resolves it server-side), so passing it on argv is safe.
        # Risk R-a: assumes the default DB service name "Postgres".
        db_ref_res = railway.set_env_var(
            "DATABASE_URL",
            "${{Postgres.DATABASE_URL}}",
            service=slug,
            cwd=str(app_dir),
        )
        if db_ref_res.get("status") == "failed":
            return _fail(
                app_slug=slug,
                checkpoint=STEP_POSTGRES,
                exit_code=3,
                error_code=db_ref_res.get("error_code", "RAILWAY_CLI_ERROR"),
                error_message=db_ref_res.get(
                    "error_message", "failed to set DATABASE_URL reference"
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        # Record only a non-secret marker that Postgres was provisioned/adopted.
        # The DATABASE_URL VALUE is never written to state (R8 entropy gate); we
        # only set a Railway reference variable above.
        state_obj.resource_ids["railway_postgres"] = "provisioned"
        _mark(STEP_POSTGRES)

    # ------------------------------------------------------------------
    # Step 7.75: Prod CORS + SPA prod redirect-URI wiring (R4 / SC3)
    # ------------------------------------------------------------------
    # Ordering decision: STEP_WIRING sits AFTER STEP_POSTGRES and BEFORE
    # STEP_TEMPLATE / git-commit / STEP_RAILWAY_DEPLOY. Two constraints pin it
    # here:
    #   1. The Railway service must already exist so it HAS a domain to capture
    #      — `railway domain` returns (generating on first call) the service
    #      domain once `railway init` (STEP_RAILWAY) has run. So wiring must be
    #      AFTER STEP_RAILWAY.
    #   2. CORS + the prod redirect URI must be correct BEFORE the app is
    #      deployed and takes its first request — so wiring must be BEFORE
    #      STEP_RAILWAY_DEPLOY (the last step). Placing it before STEP_TEMPLATE
    #      keeps it comfortably ahead of commit + deploy.
    # The Python rewrite dropped the old shell steps that did this, so prod
    # sign-in failed (SPA redirect URIs were localhost-only) and CORS was unset.
    # Gated on `with_railway`: no Railway ⇒ no prod domain ⇒ nothing to wire.
    if with_railway and not _done(STEP_WIRING):
        # act() — capture the assigned prod domain via `railway domain --json`
        # (NEVER decorated stdout — that parsing is the early live-run
        # failure class this unit kills). A capture failure is transient
        # (domain not ready yet / CLI blip) → retryable so --resume re-attempts.
        domain_res = railway.get_prod_domain(service=slug, cwd=str(app_dir))
        if domain_res.get("status") == "failed":
            return _fail(
                app_slug=slug,
                checkpoint=STEP_WIRING,
                exit_code=3,
                error_code=domain_res.get("error_code", "RAILWAY_CLI_ERROR"),
                error_message=domain_res.get(
                    "error_message", "railway domain capture failed"
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        domain = str(domain_res.get("domain"))
        prod_origin = f"https://{domain}"
        prod_redirect = f"{prod_origin}/callback"

        # act() — set CORS_ORIGINS on Railway to the prod origin so the deployed
        # app accepts cross-origin requests from its own frontend. set_env_var
        # returns a structured failure (no decorated-stdout parsing) — a CLI
        # error here is transient → retryable.
        cors_res = railway.set_env_var(
            name="CORS_ORIGINS",
            value=prod_origin,
            service=slug,
            cwd=str(app_dir),
        )
        if cors_res.get("status") == "failed":
            return _fail(
                app_slug=slug,
                checkpoint=STEP_WIRING,
                exit_code=3,
                error_code=cors_res.get("error_code", "RAILWAY_CLI_ERROR"),
                error_message=cors_res.get(
                    "error_message", "railway variables --set CORS_ORIGINS failed"
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )

        # act() — register the prod redirect URI on the SPA app (MERGE, keeping
        # localhost dev URIs). Only when there is an SPA to update: on resume
        # STEP_LOGTO is skipped so logto_result is None — adopt the SPA id from
        # the incrementally-persisted state (KTD3) instead.
        spa_app_id = (logto_result or {}).get("spa_client_id") or state_obj.resource_ids.get(
            "logto_spa_client_id"
        )
        if with_logto and spa_app_id:
            resolved_endpoint = _resolve_logto_endpoint(logto_endpoint, platform_domain)
            resolved_admin_id = (
                admin_app_id
                if admin_app_id is not None
                else os.environ.get("LOGTO_M2M_APP_ID", "")
            )
            try:
                uri_res = logto.update_app_redirect_uris(
                    endpoint=resolved_endpoint,
                    admin_app_id=resolved_admin_id,
                    spa_app_id=str(spa_app_id),
                    prod_redirect_uri=prod_redirect,
                )
            except logto.RateLimitError as exc:
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_WIRING,
                    exit_code=6,
                    error_code="LOGTO_429_RATE_LIMITED",
                    error_message=str(exc),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                )
            except logto.LogtoAuthError as exc:
                # Fix 2 (P1): a resume >~1h after STEP_LOGTO hits a stale
                # mgmt-token cache and re-auth fails — but the admin credential
                # was zeroed after the original STEP_LOGTO run and is gone from
                # memory mid-resume, so re-running can NEVER refresh the token.
                # This must be non-retryable (the general LogtoError catch below
                # would wrongly mark it retryable and let --resume loop forever).
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_WIRING,
                    exit_code=2,
                    error_code="LOGTO_AUTH_UNRECOVERABLE",
                    error_message=(
                        f"Logto admin auth failed during redirect-URI wiring "
                        f"({exc}); the admin credential is only in memory during "
                        f"the original STEP_LOGTO run and cannot be recovered "
                        f"mid-resume. Delete the state file ({state_path}) and "
                        f"re-run from scratch."
                    ),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                    retryable=False,
                )
            except logto.LogtoError as exc:
                # Transient auth/API blip (e.g. cold mgmt-token cache) → retry.
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_WIRING,
                    exit_code=3,
                    error_code="LOGTO_API_FAILED",
                    error_message=str(exc),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                )
            # assert() — the prod redirect URI MUST be present in the post-state
            # the client read back from the PATCH response. A silent no-op
            # (PATCH that didn't persist) leaves prod sign-in broken; that's a
            # STRUCTURAL failure re-running can't fix, so exit non-retryable
            # (R8) rather than let --resume loop or declare a broken app done.
            if prod_redirect not in (uri_res.get("redirect_uris") or []):
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_WIRING,
                    exit_code=3,
                    error_code="WIRING_REDIRECT_URI_MISSING",
                    error_message=(
                        f"prod redirect URI {prod_redirect!r} absent from SPA "
                        f"{spa_app_id!r} after PATCH — prod sign-in would fail"
                    ),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                    retryable=False,
                )

        # Record the (non-secret) hostname — entropy-gate safe (dots/hyphens
        # split it below the 32-char high-entropy threshold).
        state_obj.resource_ids["railway_domain"] = domain
        _mark(STEP_WIRING)

    # ------------------------------------------------------------------
    # Step 8: Render Copier template
    # ------------------------------------------------------------------
    if not _done(STEP_TEMPLATE):
        copier_data = {
            "app_name": slug,
            "app_description": description,
            "python_package_name": slug.replace("-", "_"),
            "github_user": github_owner or "",
            "platform_domain": platform_domain,
            "logto_endpoint": _resolve_logto_endpoint(logto_endpoint, platform_domain),
            "include_frontend": variant == "full" and with_frontend,
            # The mobile scaffold needs Logto and assumes Railway URLs.
            "include_mobile": (
                variant == "full"
                and with_frontend
                and with_mobile
                and provider == "logto"
                and not is_render
            ),
            "with_auth": with_auth and provider == "logto" and (with_logto or is_render),
            "with_db": with_db,
            "deploy_target": target,
            "auth_provider": provider,
        }
        try:
            template.render_template(
                template_dir=template_dir,
                output_dir=output_dir,
                data=copier_data,
                variant=variant,
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(
                app_slug=slug,
                checkpoint=STEP_TEMPLATE,
                exit_code=3,
                error_code="TEMPLATE_RENDER_FAILED",
                error_message=str(exc),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
        _mark(STEP_TEMPLATE)

    # ------------------------------------------------------------------
    # Step 8.5 (Render target): live Blueprint validation, then GitHub repo
    # ------------------------------------------------------------------
    # KTD2: `render blueprints validate` is a live, read-only API call against
    # the active workspace that preflight resolved. It creates nothing. It runs
    # BEFORE the GitHub repo is created, so an invalid Blueprint leaves no
    # repo behind. The wrapper classifies retryability (5xx / timeout only).
    # A resume validates again until the push is done, even when the
    # checkpoint is set: render.yaml or the active workspace can change
    # between runs, and the push must publish a file this run validated.
    blueprint_path = app_dir / "render.yaml"
    render_workspace_id = pf.get("render_workspace_id")
    render_workspace_name = pf.get("render_workspace_name")
    if is_render:
        if with_deploy and not _done(STEP_GIT_COMMIT):
            _progress(
                "[cyan]→[/cyan] Validating render.yaml with Render "
                f"(workspace {render_workspace_name or render_workspace_id}, read-only)..."
            )
            validate_res = render.validate_blueprint(
                str(blueprint_path), str(render_workspace_id), cwd=str(app_dir)
            )
            if validate_res.get("status") != "success":
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_RENDER_VALIDATE,
                    exit_code=3,
                    error_code=validate_res.get("error_code", "RENDER_VALIDATE_FAILED"),
                    error_message=validate_res.get(
                        "error_message", "render blueprints validate failed"
                    ),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                    retryable=bool(validate_res.get("retryable", False)),
                )
            if not _done(STEP_RENDER_VALIDATE):
                _mark(STEP_RENDER_VALIDATE)
        github_failure = _create_github_repo_step()
        if github_failure is not None:
            return github_failure

    # ------------------------------------------------------------------
    # Step 9: Push initial commit (if github)
    # ------------------------------------------------------------------
    if with_github and not _done(STEP_GIT_COMMIT):
        # Initialize and push from the rendered app directory. The GitHub URL
        # is HTTPS; using it avoids an SSH-agent prompt/hang in this
        # non-interactive pipeline. Env hygiene per R1.
        if app_dir.exists():
            _progress("[cyan]→[/cyan] Creating and pushing the initial Git commit...")
            repo_url = _repo_url()
            git_res = github.initialize_and_push_repo(
                cwd=app_dir,
                repo_url=repo_url,
                commit_message=f"Initial scaffold of {slug}",
                env=_sanitized_env(),
            )
            if git_res.get("status") == "failed":
                return _fail(
                    app_slug=slug,
                    checkpoint=STEP_GIT_COMMIT,
                    exit_code=3,
                    error_code=git_res.get("error_code", "GH_GIT_ERROR"),
                    error_message=git_res.get(
                        "error_message", "failed to push the initial Git commit"
                    ),
                    completed_steps=completed_steps,
                    state_path=state_path,
                    state_obj=state_obj,
                    raise_on_failure=raise_on_failure,
                )
        _mark(STEP_GIT_COMMIT)

    # ------------------------------------------------------------------
    # Step 10: Deploy Railway
    # ------------------------------------------------------------------
    if with_railway and not _done(STEP_RAILWAY_DEPLOY):
        # `railway up` deploys the CURRENT DIRECTORY's code and resolves the
        # project from that directory's link. Provisioning now runs from the app
        # directory too, but we still explicitly refresh the project, service,
        # and environment link before `railway up -d`. This makes resumes and
        # manually moved directories deterministic.
        project_id = state_obj.resource_ids.get("railway_project_id")
        if not project_id:
            return _fail(
                app_slug=slug,
                checkpoint=STEP_RAILWAY_DEPLOY,
                exit_code=3,
                error_code="RAILWAY_PROJECT_ID_MISSING",
                error_message=(
                    "cannot link the app dir for deploy: no railway_project_id "
                    "in state (railway init did not capture it)."
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
        _progress("[cyan]→[/cyan] Linking the app directory to Railway...")
        link_res = railway.link_project(
            project_id=project_id, service=slug, cwd=str(app_dir)
        )
        if link_res.get("status") == "failed":
            return _fail(
                app_slug=slug,
                checkpoint=STEP_RAILWAY_DEPLOY,
                exit_code=3,
                error_code=link_res.get("error_code", "RAILWAY_CLI_ERROR"),
                error_message=link_res.get("error_message", "railway link failed"),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        _progress("[cyan]→[/cyan] Uploading the application to Railway...")
        deploy_res = railway.deploy(detached=True, service=slug, cwd=str(app_dir))
        if deploy_res.get("status") == "failed":
            return _fail(
                app_slug=slug,
                checkpoint=STEP_RAILWAY_DEPLOY,
                exit_code=3,
                error_code=deploy_res.get("error_code", "RAILWAY_CLI_ERROR"),
                error_message=deploy_res.get(
                    "error_message", "railway up failed"
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        _progress("[cyan]→[/cyan] Waiting for Railway to build and deploy...")
        wait_res = railway.wait_for_deployment(
            service=slug,
            cwd=str(app_dir),
            on_status=lambda status: _progress(
                f"    Railway deployment status: [bold]{status}[/bold]"
            ),
        )
        if wait_res.get("status") == "failed":
            return _fail(
                app_slug=slug,
                checkpoint=STEP_RAILWAY_DEPLOY,
                exit_code=3,
                error_code=wait_res.get("error_code", "RAILWAY_DEPLOY_FAILED"),
                error_message=wait_res.get(
                    "error_message", "Railway deployment failed"
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
            )
        _mark(STEP_RAILWAY_DEPLOY)

    # ------------------------------------------------------------------
    # Step 10.5: Final verify() gate — health + auth (R5 / R8 / KTD6)
    # ------------------------------------------------------------------
    # "Success" must mean the deployed app actually responds and its auth works
    # — not that the pipeline printed a summary. This runs AFTER the deploy is
    # marked done and BEFORE the success/state-delete below, so the state file
    # only vanishes once verify passes. Gated on `with_railway` (nothing was
    # deployed otherwise, so there is nothing to probe).
    #
    # Retryable choice (health-exhausted): a `/health` that never returns 200
    # within the poll bound is TRANSIENT → retryable. The Railway deployment
    # has already reached SUCCESS here, but a slow application startup / cold
    # start can still outlast a bounded health poll; the correct recovery is to
    # `--resume` once it settles (STEP_RAILWAY_DEPLOY is already marked, so
    # resume jumps straight back to verify — cheap). `--resume` is
    # operator-invoked, not automatic, so a genuinely-dead app cannot
    # infinite-loop on its own. By contrast an auth 401 is STRUCTURAL → the
    # deployed app's JWT config is wrong and re-running can't fix it, so it exits
    # non-retryable (R8) and `--resume` refuses.
    verify_summary: dict[str, Any] | None = None
    if with_railway and not _done(STEP_VERIFY):
        _progress("[cyan]→[/cyan] Verifying the production deployment...")
        base_domain = state_obj.resource_ids.get("railway_domain")
        if not base_domain:
            # Fix 4 (P2): `with_railway` means an app was deployed, so STEP_WIRING
            # MUST have persisted a prod domain to probe. If it is missing we
            # would otherwise fall through and mark verify "done" WITHOUT any
            # health/auth check — the silent-success class R5 exists to kill.
            # Fail loud, non-retryable (re-running can't conjure a domain that
            # wiring never recorded — the run is structurally broken).
            return _fail(
                app_slug=slug,
                checkpoint=STEP_VERIFY,
                exit_code=3,
                error_code="VERIFY_NO_DOMAIN",
                error_message=(
                    "no prod domain recorded in state to verify against, but "
                    "--railway implies a deployed app — refusing to mark verify "
                    "done without a health/auth check (that would be silent "
                    "success). Delete the state file and re-run from scratch."
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
        base_url = f"https://{base_domain}"
        # Build the app-token minter only when the app's OWN M2M creds are
        # available in THIS process. On a fresh run `logto_result` carries
        # the just-minted `m2m_app_secret` (the orchestrator zeros the
        # *admin* credential after STEP_LOGTO, not this app secret, which
        # lives as a plain str in the result dict). On a --resume STEP_LOGTO
        # is skipped so `logto_result` is None and the app secret is NOT in
        # state (the R8 entropy gate forbids persisting it) — so we fall back
        # to a health-only verify. The auth wiring itself was already
        # asserted structurally in STEP_WIRING (U6), so health-only on resume
        # is an acceptable degrade.
        mint_token = None
        if (
            with_logto
            and logto_result
            and logto_result.get("m2m_app_id")
            and logto_result.get("m2m_app_secret")
            and logto_result.get("api_resource")
        ):
            mint_token = verify.make_m2m_token_minter(
                endpoint=(
                    logto_result.get("endpoint")
                    or _resolve_logto_endpoint(logto_endpoint, platform_domain)
                ),
                client_id=str(logto_result["m2m_app_id"]),
                client_secret=str(logto_result["m2m_app_secret"]),
                resource=str(logto_result["api_resource"]),
            )
        verify_result = verify.verify_deployment(base_url, mint_token=mint_token)
        v_status = verify_result.get("status")
        if v_status == verify.STRUCTURAL:
            # Broken app (e.g. auth rejected) — re-running won't fix it.
            return _fail(
                app_slug=slug,
                checkpoint=STEP_VERIFY,
                exit_code=3,
                error_code=verify_result.get("error_code", "VERIFY_FAILED"),
                error_message=verify_result.get(
                    "error_message",
                    verify_result.get("message", "verify structural failure"),
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
                retryable=False,
            )
        if v_status == verify.TRANSIENT:
            # Health not up in bound / mint blip — retry once it settles.
            return _fail(
                app_slug=slug,
                checkpoint=STEP_VERIFY,
                exit_code=3,
                error_code=verify_result.get("error_code", "VERIFY_FAILED"),
                error_message=verify_result.get(
                    "error_message",
                    verify_result.get("message", "verify transient failure"),
                ),
                completed_steps=completed_steps,
                state_path=state_path,
                state_obj=state_obj,
                raise_on_failure=raise_on_failure,
                retryable=True,
            )
        # Only reached on verify success (the no-domain case fails loud above).
        # Fix #3: record whether auth was actually re-checked. On a --resume the
        # app's M2M secret is gone (never persisted), so mint_token was None and
        # verify ran health-only → auth_checked False. Surfacing this in the
        # envelope keeps a resumed run honest: it is NOT fully auth-verified.
        verify_summary = {"auth_checked": bool(verify_result.get("auth_checked", False))}
        _mark(STEP_VERIFY)

    # ------------------------------------------------------------------
    # Step 11: Success — clean up state, return envelope
    # ------------------------------------------------------------------
    render_block: dict[str, Any] | None = None
    if is_render:
        validated = _done(STEP_RENDER_VALIDATE)
        blueprint_url: str | None = None
        if with_github:
            # The repo exists and holds the pushed scaffold (Steps 4 and 9).
            repo_url = _repo_url()
            blueprint_url = f"https://render.com/deploy?repo={repo_url}"
            next_step = (
                "Open blueprint_url and apply the Blueprint in the Render "
                "dashboard. Applying it creates paid Render resources."
            )
        else:
            next_step = (
                "Push this app to a GitHub repo, then open "
                "https://render.com/deploy?repo=<github-repo-url> or use "
                f"{RENDER_DASHBOARD_PATH} in the Render dashboard to apply "
                "render.yaml."
            )
        if not validated:
            next_step = (
                "Validate the Blueprint first with `render blueprints validate "
                "render.yaml`. " + next_step
            )
        render_block = {
            "workspace": render_workspace_name,
            "workspace_id": render_workspace_id,
            "blueprint_path": str(blueprint_path),
            "validated": validated,
            "status": "awaiting_blueprint_apply",
            "blueprint_url": blueprint_url,
            "next_step": next_step,
        }

    try:
        state.delete_state(state_path)
    except Exception:  # noqa: BLE001 — non-fatal
        pass

    return _success_envelope(
        app_slug=slug,
        variant=variant,
        scaffolded_by=scaffolded_by,
        logto_result=logto_result,
        github_result=github_result,
        railway_result=railway_result,
        railway_workspace=railway_workspace,
        completed_steps=completed_steps,
        dry_run=False,
        verify_summary=verify_summary,
        deploy_target=target,
        render_block=render_block,
        auth_provider=provider,
    )
