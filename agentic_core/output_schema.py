"""Pydantic v2 models for the ``agentic-core create-app`` JSON contract.

Spec §R12 formalizes the create-app JSON envelopes as Pydantic models so
that:

  - The Typer command layer (``agentic_core.cli``) gets free validation
    when constructing the envelope — accidental shape drift fails fast
    in tests rather than silently producing a malformed contract that
    breaks agent callers downstream.
  - The schema is a single, importable source of truth for agent callers to
    introspect.
  - JSON serialization preserves shape across CPython versions.

Two envelopes are defined:

  - :class:`SuccessEnvelope` — emitted on a clean ``create-app`` run.
    Mirrors §A2's success shape with ``schema_version: 1`` pinned and the
    R10 variant-skip semantic (Logto/GitHub/Railway sub-blocks may be
    ``None`` when the corresponding ``--no-*`` flag was passed or when
    the variant doesn't need that provisioning step).

  - :class:`FailureEnvelope` — emitted on any non-zero exit when
    ``--json`` is set. Mirrors §R3's shape and carries the R4-aligned
    ``exit_code`` field so callers can both read the JSON and exit with
    the matching shell code.

Schema versioning (R12):

  - ``schema_version`` is the single source of truth. Today only ``1``
    exists.
  - The CLI takes ``--api-version N`` (default 1). When N != 1, the CLI
    rejects with a clear error pointing at the changelog — bumping the
    version requires agentic-core and its agent callers to
    coordinate. There is no "emit both shapes" mode (structurally
    impossible for a single JSON object).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


#: The only schema version that exists today. Bumping this breaks agent
#: callers that parse v1 — see §R12.
CURRENT_SCHEMA_VERSION: int = 1


class SuccessEnvelope(BaseModel):
    """§A2 / §R3 success shape — emitted on a clean ``create-app`` run.

    Sub-blocks (``logto``, ``github``, ``railway``) are ``None`` when the
    corresponding provisioning step was skipped via ``--no-auth``,
    ``--no-github``, ``--no-railway``, or by the R10 variant matrix
    (e.g. ``static-dashboard --no-auth`` short-circuits Logto entirely).

    ``cli`` describes the per-app CLI shipped by the template (binary
    name + env-var prefix); populated whenever the variant ships an
    ``app/cli/`` subpackage (i.e. variant != ``static-dashboard --no-auth``).

    ``deploy_target`` is ``railway`` or ``render``. The ``railway`` block is
    ``None`` on Render. The following blocks describe manual setup:

    - ``render`` (Render only): ``workspace``, ``workspace_id``, ``blueprint_path``,
      ``validated``, ``status`` (``awaiting_blueprint_apply``, or ``dry_run``),
      ``blueprint_url`` (``None`` when this run did not create the GitHub
      repo), and ``next_step``.
    - ``auth`` (Google or manual Logto setup): ``provider``, ``status`` (not configured),
      ``required_settings`` (setting names the operator must set, never
      values), ``limitations`` (what the app does not support yet), and
      ``next_step``.

    The model uses ``extra="allow"`` so the orchestrator can add fields
    over time (e.g. ``railway.url``, ``logto.scopes``) without forcing a
    schema bump for additive changes. Removing or renaming a field is
    still a breaking change and requires a ``schema_version`` bump.
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = CURRENT_SCHEMA_VERSION
    status: Literal["success"] = "success"
    app_slug: str
    variant: str
    scaffolded_by: Literal["agent", "manual"]
    logto: dict[str, Any] | None = None
    github: dict[str, Any] | None = None
    railway: dict[str, Any] | None = None
    deploy_target: Literal["railway", "render"] | None = None
    render: dict[str, Any] | None = None
    auth: dict[str, Any] | None = None
    cli: dict[str, Any] | None = None
    completed_steps: list[str] = []
    dry_run: bool = False


class FailureEnvelope(BaseModel):
    """§R3 failure shape — emitted on any non-zero exit when ``--json`` set.

    ``exit_code`` mirrors §R4's contract (1 user error, 2 auth, 3
    provisioning, 4 network, 5 misconfig, 6 rate-limited) so callers can
    cross-reference the JSON envelope with the process exit code.

    ``error_code`` is a short, stable, machine-readable token (e.g.
    ``INVALID_SLUG``, ``LOGTO_429_RATE_LIMITED``, ``KEYSTORE_DECRYPT_FAILED``).
    ``error_message`` is the human-readable explanation.

    ``completed_steps`` lets ``--resume`` callers know what was already
    done before the failure. Empty list = nothing got committed.

    ``retryable`` (R8) tells ``--resume`` whether re-attempting is worth it.
    Defaults to ``True`` — transient failures (rate limits, network, auth
    hiccups) resume cleanly. Structural failures (invalid slug/variant,
    corrupt state, a template that won't render into a working app) set it
    to ``False`` so ``--resume`` refuses to loop on a genuinely broken app.
    Additive optional field with a default — no ``schema_version`` bump
    (the file's ``extra="allow"`` + default convention covers it).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = CURRENT_SCHEMA_VERSION
    # ``failed`` is the canonical value every emitter writes
    # (orchestrator._failure_envelope, github._fail, railway._fail, the
    # admin CLI's output.emit_failure). Pinning the Literal to a single
    # value catches future drift at validation time rather than tolerating
    # the historic ``failure`` alias.
    status: Literal["failed"] = "failed"
    app_slug: str
    variant: str | None = None
    checkpoint: str | None = None
    exit_code: int
    error_code: str
    error_message: str
    completed_steps: list[str] = []
    retryable: bool = True


class ListAppsEntry(BaseModel):
    """One sibling app's registry entry as emitted by ``agentic-core list-apps``.

    Built purely from the on-disk ``.bootstrap-state.json`` file — §R18
    forbids live status checks (no HTTP, no shelling out to ``gh`` / ``railway``).
    The ``logto`` / ``github`` / ``railway`` sub-blocks are ``None`` when no
    corresponding resource IDs were ever persisted (e.g. ``--no-auth`` runs
    leave ``logto`` empty).

    ``state_file`` is the absolute path to the source state file — agents use
    this for `--resume` invocations or for cross-referencing the on-disk file.
    """

    model_config = ConfigDict(extra="allow")

    slug: str
    variant: str
    scaffolded_by: Literal["agent", "manual"]
    logto: dict[str, Any] | None = None
    railway: dict[str, Any] | None = None
    github: dict[str, Any] | None = None
    created_at: datetime
    state_file: str


class ListAppsEnvelope(BaseModel):
    """§R18 registry-lister envelope for ``agentic-core list-apps``.

    Pure registry view — derived entirely from sibling-directory state files
    next to the agentic-core repo. No live status calls. Empty ``apps`` list
    is a valid response (no siblings yet, or scan target missing).
    """

    model_config = ConfigDict(extra="allow")

    schema_version: int = CURRENT_SCHEMA_VERSION
    status: Literal["success"] = "success"
    apps: list[ListAppsEntry] = []
    scanned_dir: str
