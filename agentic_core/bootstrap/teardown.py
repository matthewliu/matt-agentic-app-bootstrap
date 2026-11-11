"""Manual teardown escape hatch (U8 / R7 / KTD5).

``teardown_app`` reads an app's ``.bootstrap-state.json`` and deletes the
per-app resources a bootstrap run created:

  - Logto SPA application, M2M application, API resource, and user.
  - The Railway project (which subsumes its Postgres service + domain).

It is a MANUAL, opt-in escape hatch for a wedged run — never automatic
rollback. Three HARD INVARIANTS (KTD5) are enforced here, not just
documented:

1. **GitHub is never deleted.** This module never imports the GitHub client
   and never issues a ``gh`` command. ``github_repo_url`` is reported under
   ``preserved`` — a repo is cheap to keep and painful to lose.

2. **The shared ``agentic-core-admin`` app is never a delete target.** Its
   app id (``LOGTO_M2M_APP_ID`` env, or the ``admin_app_id`` arg) is checked
   against every Logto id BEFORE any DELETE. Deleting it would break the whole
   bootstrap pipeline (see ``CLAUDE.md``). Any id matching it is skipped and
   reported under ``skipped_admin`` — even if it erroneously appears in an
   app's ``resource_ids``.

3. **Confirmation is always required.** Unless ``assume_yes`` is True, a
   ``confirm`` callback must return truthy or NOTHING is deleted. With no
   confirmation mechanism and no ``assume_yes``, teardown aborts (deletes
   nothing) — the safe default. The interactive prompt itself lives in the
   CLI (``typer.confirm``); this layer is the programmatic guard.

Partial state (some ids missing) deletes what exists and reports the rest as
``already_absent`` — a re-run over a half-torn-down app is a safe no-op.

Credential note: like ``logto.update_app_redirect_uris``, this constructs the
Logto client in cache-only mode (empty ``bytearray`` credential) by default,
relying on the on-disk management-token cache. Standalone teardown run long
after provisioning may hit a cold cache; pass ``admin_credential`` (or warm
the cache) in that case.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from collections.abc import Callable

from agentic_core import list_apps
from agentic_core.bootstrap import logto, railway
from agentic_core.bootstrap import state as state_mod
from agentic_core.output_schema import CURRENT_SCHEMA_VERSION

#: Logto application-id resource keys (deleted via ``delete_application``).
_LOGTO_APP_KEYS = ("logto_spa_client_id", "logto_m2m_app_id")

#: Resource-id keys that are DELIBERATELY preserved (never a delete target).
_PRESERVED_KEYS = ("github_repo_url",)

#: Slug format guard. teardown is a DESTRUCTIVE escape hatch and the slug is
#: joined into a filesystem Path (``<root>/<slug>/.bootstrap-state.json``), so
#: an unvalidated ``../../evil`` would escape the sibling-apps directory — and
#: ``--yes`` skips the confirmation prompt that would otherwise reveal it.
#: This MIRRORS ``orchestrator.SLUG_REGEX`` (agentic_core/bootstrap/
#: orchestrator.py:102); re-declared here (not imported) to keep this module
#: out of the orchestrator import graph. Keep the two in sync.
SLUG_REGEX = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")


def _resolve_state_path(
    slug: str, state_path: Path | str | None, root: Path | str | None
) -> Path:
    """Locate the app's state file: explicit override wins, else sibling scan."""
    if state_path is not None:
        return Path(state_path)
    base = list_apps._resolve_sibling_root(Path(root) if root is not None else None)
    return base / slug / ".bootstrap-state.json"


def teardown_app(
    slug: str,
    *,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    state_path: Path | str | None = None,
    root: Path | str | None = None,
    admin_app_id: str | None = None,
    admin_credential: bytearray | None = None,
    logto_endpoint: str | None = None,
    platform_domain: str | None = None,
) -> dict[str, Any]:
    """Delete the per-app Logto + Railway resources recorded in state.

    Args:
      slug: The app slug to tear down.
      assume_yes: Skip the confirmation gate (the CLI passes this after its
        own ``typer.confirm``). Default False.
      confirm: Optional ``(message) -> bool`` confirmation callback. Consulted
        only when ``assume_yes`` is False. Returning falsy — or being absent —
        aborts with zero deletes.
      state_path: Explicit state-file path (tests). Defaults to the sibling
        ``<root>/<slug>/.bootstrap-state.json``.
      root: Override the sibling scan root (tests / agent-sandbox layouts).
      admin_app_id: The shared admin app id to guard. Defaults to the
        ``LOGTO_M2M_APP_ID`` env var.
      admin_credential: Optional live admin secret; defaults to cache-only.
      logto_endpoint: Logto base URL. When ``None``, resolves from the
        ``LOGTO_ENDPOINT`` env var (matching preflight/orchestrator), and only
        then falls back to the constructed ``https://auth.<platform_domain>``.
      platform_domain: Platform domain for the constructed fallback endpoint
        (used only when neither ``logto_endpoint`` nor ``LOGTO_ENDPOINT`` is set).
        ``None`` ⇒ the ``PLATFORM_DOMAIN`` env var. With none of the three,
        a teardown with Logto resources aborts before any delete.

    Returns:
      An envelope-shaped dict with a ``teardown`` sub-block:
      ``{deleted, already_absent, skipped_admin, preserved, failed, aborted,
      confirmed, aborted_reason}``. Lists use the ``resource_ids`` KEY names
      (e.g. ``"logto_spa_client_id"``); ``skipped_admin`` holds the offending
      ids. The top-level ``status`` is ``"failed"`` when any delete failed OR a
      guard refusal set ``aborted_reason``; otherwise ``"success"`` (including a
      declined-confirmation abort, where nothing was attempted).

    Raises:
      ValueError: if ``slug`` does not match :data:`SLUG_REGEX`. Validated
        BEFORE any path resolution so a traversal slug can never touch the
        filesystem.
    """
    # Fix 2 — validate the slug BEFORE it is joined into a Path. Fail loud and
    # touch nothing on a malformed/traversal slug.
    if not isinstance(slug, str) or not SLUG_REGEX.match(slug):
        raise ValueError(
            f"invalid app slug {slug!r}: must match {SLUG_REGEX.pattern}"
        )

    resolved_state = _resolve_state_path(slug, state_path, root)
    state_obj = state_mod.read_state(resolved_state)
    rids = dict(state_obj.resource_ids)

    preserved = [k for k in _PRESERVED_KEYS if rids.get(k)]

    def _summary(
        *,
        deleted: list[str],
        already_absent: list[str],
        skipped_admin: list[str],
        failed: list[str],
        aborted: bool,
        confirmed: bool | None = None,
        aborted_reason: str | None = None,
    ) -> dict[str, Any]:
        # A teardown is a FAILURE if any resource delete failed, OR if we
        # refused to proceed for a reason other than a declined confirmation
        # (``aborted_reason`` set — e.g. the admin guard could not be resolved).
        # A DECLINED confirmation (aborted=True, aborted_reason=None) stays a
        # success: the operator chose to stop and nothing failed.
        status = "failed" if (failed or aborted_reason) else "success"
        return {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "status": status,
            "app_slug": state_obj.app_slug,
            "variant": state_obj.variant,
            "scaffolded_by": state_obj.scaffolded_by,
            "teardown": {
                "deleted": deleted,
                "already_absent": already_absent,
                "skipped_admin": skipped_admin,
                "preserved": preserved,
                "failed": failed,
                "aborted": aborted,
                # A guard-refusal is aborted-yet-confirmed, so let callers pass
                # ``confirmed`` explicitly; default to the "unconfirmed abort"
                # meaning for the confirmation gate below.
                "confirmed": (not aborted) if confirmed is None else confirmed,
                # Non-None only when teardown refused to proceed for a reason
                # other than a declined confirmation (e.g. admin-guard unresolved).
                "aborted_reason": aborted_reason,
            },
            "dry_run": False,
        }

    # ------------------------------------------------------------------
    # Fix 6 — the loaded state MUST belong to the requested slug. A stale,
    # copied, or wrong state file (e.g. a `.bootstrap-state.json` from a
    # different app dropped into `<slug>/`) would otherwise make
    # `teardown <slug>` delete a DIFFERENT app's Logto + Railway resources.
    # Refuse BEFORE the confirmation gate and any delete. GitHub is preserved
    # regardless (never a delete target).
    # ------------------------------------------------------------------
    if state_obj.app_slug != slug:
        return _summary(
            deleted=[],
            already_absent=[],
            skipped_admin=[],
            failed=[],
            aborted=True,
            confirmed=False,
            aborted_reason=(
                f"slug_mismatch: state file at {resolved_state} records "
                f"app_slug={state_obj.app_slug!r}, not {slug!r}. Refusing to "
                "tear down to avoid deleting the wrong app's resources."
            ),
        )

    # ------------------------------------------------------------------
    # INVARIANT 3 — confirmation gate. Nothing below runs until confirmed.
    # ------------------------------------------------------------------
    confirmed = assume_yes
    if not confirmed and confirm is not None:
        confirmed = bool(
            confirm(
                f"Delete ALL Logto + Railway resources for {slug!r}? "
                "This cannot be undone."
            )
        )
    if not confirmed:
        return _summary(
            deleted=[], already_absent=[], skipped_admin=[], failed=[], aborted=True
        )

    resolved_admin_id = (
        admin_app_id
        if admin_app_id is not None
        else os.environ.get("LOGTO_M2M_APP_ID", "")
    )

    logto_keys = (*_LOGTO_APP_KEYS, "logto_api_resource", "logto_user_sub")
    has_logto = any(rids.get(k) for k in logto_keys)

    # ------------------------------------------------------------------
    # INVARIANT 2 (FAIL CLOSED) — the admin-app guard is only meaningful if we
    # can resolve the admin app id. If it is empty (LOGTO_M2M_APP_ID unset AND
    # no admin_app_id passed), we CANNOT prove a Logto delete target isn't the
    # shared ``agentic-core-admin`` app, so we MUST NOT proceed treating
    # "unknown admin id" as "nothing is admin" — that would silently turn the
    # KTD5 hard invariant into a no-op. teardown is a manual escape hatch often
    # run from a shell without ``.env`` sourced, so this is a live risk. Refuse
    # the whole destructive command (Logto AND Railway) rather than delete
    # blind. GitHub is preserved regardless.
    # ------------------------------------------------------------------
    if has_logto and not resolved_admin_id:
        return _summary(
            deleted=[],
            already_absent=[],
            skipped_admin=[],
            failed=[],
            aborted=True,
            confirmed=True,  # the operator DID confirm; we refused on the guard
            aborted_reason=(
                "admin_guard_unresolved: cannot verify the admin-app guard — "
                "LOGTO_M2M_APP_ID is unset and no admin_app_id was passed. "
                "Refusing to delete Logto resources. Set LOGTO_M2M_APP_ID (or "
                "pass --admin-app-id / admin_app_id) and re-run."
            ),
        )

    # Endpoint precedence mirrors preflight/orchestrator: an explicit arg
    # wins, else the LOGTO_ENDPOINT env var (the real self-hosted Logto URL —
    # see .env.example), else https://auth.<platform_domain>. There is no
    # built-in domain, so with none of these the endpoint is unknown. Refuse
    # before any delete (Logto or Railway), like the admin guard above.
    domain = (platform_domain or os.environ.get("PLATFORM_DOMAIN", "")).strip()
    endpoint = (
        logto_endpoint
        or os.environ.get("LOGTO_ENDPOINT")
        or (f"https://auth.{domain}" if domain else "")
    )
    if has_logto and not endpoint:
        return _summary(
            deleted=[],
            already_absent=[],
            skipped_admin=[],
            failed=[],
            aborted=True,
            confirmed=True,
            aborted_reason=(
                "logto_endpoint_unresolved: this app has Logto resources, but "
                "LOGTO_ENDPOINT and PLATFORM_DOMAIN are unset. Set "
                "LOGTO_ENDPOINT in .env and re-run."
            ),
        )

    def _is_admin(candidate: str) -> bool:
        # INVARIANT 2 — never let the shared admin app id become a target.
        return bool(resolved_admin_id) and candidate == resolved_admin_id

    deleted: list[str] = []
    already_absent: list[str] = []
    skipped_admin: list[str] = []
    failed: list[str] = []

    # ------------------------------------------------------------------
    # Logto: delete SPA/M2M apps by id, API resource + user by resolved id.
    # ------------------------------------------------------------------
    if has_logto:
        client = logto.LogtoManagementClient(
            endpoint=endpoint,
            admin_app_id=resolved_admin_id,
            admin_credential=(
                admin_credential if admin_credential is not None else bytearray()
            ),
        )

        # SPA + M2M applications (ids stored directly).
        for key in _LOGTO_APP_KEYS:
            app_id = rids.get(key)
            if not app_id:
                already_absent.append(key)
                continue
            if _is_admin(app_id):
                skipped_admin.append(app_id)
                continue
            try:
                present = client.delete_application(app_id)
            except logto.LogtoError:
                failed.append(key)
                continue
            (deleted if present else already_absent).append(key)

        # API resource: state records the INDICATOR URL — resolve to internal id.
        indicator = rids.get("logto_api_resource")
        if not indicator:
            already_absent.append("logto_api_resource")
        elif _is_admin(indicator):
            skipped_admin.append(indicator)
        else:
            res = client.find_resource(indicator)
            res_id = res.get("id") if res else None
            if not res_id:
                already_absent.append("logto_api_resource")
            elif _is_admin(str(res_id)):
                skipped_admin.append(str(res_id))
            else:
                try:
                    present = client.delete_api_resource(str(res_id))
                    (deleted if present else already_absent).append("logto_api_resource")
                except logto.LogtoError:
                    failed.append("logto_api_resource")

        # User: state records the USERNAME — resolve to internal id.
        username = rids.get("logto_user_sub")
        if not username:
            already_absent.append("logto_user_sub")
        else:
            user = client.find_user(username)
            user_id = user.get("id") if user else None
            if not user_id:
                already_absent.append("logto_user_sub")
            elif _is_admin(str(user_id)):
                skipped_admin.append(str(user_id))
            else:
                try:
                    present = client.delete_user(str(user_id))
                    (deleted if present else already_absent).append("logto_user_sub")
                except logto.LogtoError:
                    failed.append("logto_user_sub")
    else:
        already_absent.extend(logto_keys)

    # ------------------------------------------------------------------
    # Railway: delete the project (subsumes Postgres + domain). Never GitHub.
    # ------------------------------------------------------------------
    project_id = rids.get("railway_project_id")
    if not project_id:
        already_absent.append("railway_project_id")
    else:
        result = railway.delete_project(project_id=project_id)
        if result.get("status") == "success":
            deleted.append("railway_project_id")
        else:
            failed.append("railway_project_id")

    summary = _summary(
        deleted=deleted,
        already_absent=already_absent,
        skipped_admin=skipped_admin,
        failed=failed,
        aborted=False,
    )

    # Fix 7 — clear the state file after a fully successful teardown so a later
    # `create-app --resume` does not chase resources this run just deleted. Only
    # on success: a partial-failure teardown (status "failed") keeps the file so
    # the operator can retry, and the refused/declined paths returned earlier
    # (leaving the file untouched) never reach here.
    if summary["status"] == "success":
        state_mod.delete_state(resolved_state)

    return summary
