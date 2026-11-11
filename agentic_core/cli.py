"""Top-level Typer app for the ``agentic-core`` bootstrap CLI.

Mirrors the per-app template pattern
(``templates/mini-app/template/{{ app_name }}/app/cli/main.py.jinja``):
a single root Typer with a global ``--json`` / ``--debug`` flag callback
and subcommands registered directly on the root.

Subcommands today:

  - ``agentic-core --version`` — print the version, exit.
  - ``agentic-core agent-context`` — emit the agent-context v1 envelope (stub today;
    task #10 populates real Typer introspection).
  - ``agentic-core create-app SLUG ...`` — scaffold + provision + deploy a
    new mini-app (task #9). Wraps :func:`agentic_core.bootstrap.orchestrator.
    orchestrate_create_app` with R11 canonical flags, R12 schema versioning,
    R4 exit-code translation, and R2 stdout discipline.
  - ``agentic-core admin init-keystore`` — write the encrypted Logto admin
    keystore (task #7).
  - ``agentic-core list-apps`` — registry lister (task #10).

Stdout discipline (R2): the root callback stashes the ``--json`` boolean on
``ctx.obj``. Subcommands check it and route ALL human output via
:mod:`agentic_core.output`'s ``console`` (stderr-bound). In JSON mode,
``stdout`` is reserved for exactly one JSON line produced by ``render_json``.

Exception handling: the ``create-app`` callback wraps the orchestrator in
a try/except that translates any unhandled exception into a §R3 failure
envelope (exit code 3, error_code ``UNHANDLED_EXCEPTION``). A Python
traceback on stdout would corrupt the contract agent callers read,
so this safety net is load-bearing.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from agentic_core import __version__, agent_context, list_apps, output
from agentic_core.admin import admin_app
from agentic_core.bootstrap import keystore, orchestrator, preflight, teardown
from agentic_core.output_schema import (
    CURRENT_SCHEMA_VERSION,
    FailureEnvelope,
    SuccessEnvelope,
)



class DeployTarget(StrEnum):
    """``--deploy-target`` choices (KTD11)."""

    render = "render"
    railway = "railway"


class AuthProvider(StrEnum):
    google = "google"
    logto = "logto"


def _from_command_line(ctx: typer.Context, name: str) -> bool:
    """True when parameter ``name`` was given on the command line.

    KTD11: an env-sourced value (for example an exported or ``.env``
    ``RAILWAY_WORKSPACE``) must never select or block a deploy target, so
    target resolution reads Click's parameter source, not the value.
    """
    source = ctx.get_parameter_source(name)
    return source is not None and source.name == "COMMANDLINE"


app = typer.Typer(
    name="agentic-core",
    help=(
        "Scaffold new apps with Google sign-in and Render by default. "
        "Use --json for machine-readable output."
    ),
    no_args_is_help=True,
)

# `admin` subcommand group — one-time operator commands. Mounted here so
# the root `--json` flag from the global callback propagates into
# subcommands like `admin init-keystore` via `ctx.obj["json"]`.
app.add_typer(admin_app, name="admin")


def _load_env_files() -> None:
    """Load credential env files so ``agentic-core`` works without the caller
    first running ``set -a; source .env``.

    Precedence, highest first: already-exported shell vars > ``./.env`` >
    ``~/.agentic-apps.env``. ``load_dotenv(override=False)`` never clobbers a
    value already present in ``os.environ``, so loading the higher-priority
    file first makes it win. Missing files are silently skipped. This mirrors
    the generated app's pydantic ``env_file=(~/.agentic-apps.env, ".env")``
    resolution, keeping the CLI and the deployed backend on one rule.
    """
    from dotenv import load_dotenv

    load_dotenv(Path.cwd() / ".env", override=False)
    load_dotenv(Path.home() / ".agentic-apps.env", override=False)


def _version_callback(value: bool) -> None:
    if value:
        # Plain stdout for `--version`: matches the de-facto convention for
        # tools used in shell pipelines (`agentic-core --version | awk ...`).
        # This is a special case — NOT JSON mode — so writing to stdout is
        # correct and expected.
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        False,
        "--json",
        help=(
            "Emit a single JSON object to stdout (machine-readable). All "
            "Rich/log output routes to stderr. Agents use this mode."
        ),
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Verbose logging to stderr (secrets always redacted).",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print the agentic-core version and exit.",
    ),
) -> None:
    """agentic-core — global flags.

    Subcommands (create-app, list-apps, admin ...) are mounted in later tasks.
    """
    # `version` is consumed by `_version_callback` via `is_eager=True` — by
    # the time we get here, the version path has already exited. Naming the
    # parameter keeps it visible in `--help`.
    del version
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_output
    ctx.obj["debug"] = debug


@app.command("agent-context")
def agent_context_cmd(
    ctx: typer.Context,
    json_output: bool = typer.Option(
        True,
        "--json/--no-json",
        help=(
            "Emit JSON to stdout (default). --no-json is reserved for future "
            "human-readable rendering; today there is no pretty mode."
        ),
    ),
) -> None:
    """Emit the agent-context v1 envelope: every command, arg, and exit code.

    Coding agents read this once per session to discover the CLI surface
    without hard-coding command names. Today the envelope is a stub with an
    empty ``commands`` list (task #10 wires real Typer introspection); the
    shape is stable so downstream parsers won't break when commands populate.

    Always JSON (machine-only). The ``--json`` flag is accepted both here and
    on the root callback (``agentic-core --json agent-context``) for
    consistency with sibling subcommands; today both routes emit the same
    single JSON line on stdout.
    """
    # `ctx` is unused today — the envelope shape is fixed and doesn't depend
    # on global flags. Kept for symmetry with future subcommands and so that
    # task #10's introspection can read sibling commands off `ctx.find_root()`.
    # `json_output` is currently a no-op (there is no pretty mode); accepting
    # the flag keeps the agent-discoverable surface uniform.
    del ctx, json_output
    data = agent_context.generate(app, version=__version__)
    output.render_json(data)


# ---------------------------------------------------------------------------
# create-app — Task #9
# ---------------------------------------------------------------------------


def _emit_failure(
    *,
    json_mode: bool,
    app_slug: str,
    variant: str | None,
    checkpoint: str | None,
    exit_code: int,
    error_code: str,
    error_message: str,
    completed_steps: list[str] | None = None,
    retryable: bool = True,
    extra: dict[str, Any] | None = None,
) -> None:
    """Render a failure consistently in pretty or JSON mode.

    In JSON mode: writes a single §R3 envelope line to stdout. In pretty
    mode: writes a Rich error panel to stderr. Either way, the caller
    follows up with ``raise typer.Exit(exit_code)``.

    ``retryable`` (Fix 10) is threaded onto the emitted envelope so callers
    (agent callers / ``--resume``) can tell a deterministic failure
    (bad flags, invalid slug, a failed preflight) from a transient one.
    Deterministic CLI failures pass ``retryable=False``; genuinely transient
    ones keep the ``True`` default.

    ``extra`` (Fix 23) merges additional fields into the JSON envelope — used
    by teardown to preserve its per-resource summary on the failure path. The
    ``FailureEnvelope`` model allows extra fields (``extra="allow"``).
    """
    if json_mode:
        envelope = FailureEnvelope(
            app_slug=app_slug or "",
            variant=variant,
            checkpoint=checkpoint,
            exit_code=exit_code,
            error_code=error_code,
            error_message=error_message,
            completed_steps=completed_steps or [],
            retryable=retryable,
        )
        payload = envelope.model_dump(mode="json")
        if extra:
            payload.update(extra)
        output.render_json(payload)
    else:
        output.console.print(
            f"[red]Error[/red] ({error_code}, exit {exit_code}): {error_message}"
        )


@app.command("create-app")
def create_app_cmd(  # noqa: PLR0913 — Typer params, not internal kwargs
    ctx: typer.Context,
    slug: str = typer.Argument(
        ..., help="App slug (lowercase, ^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$)."
    ),
    description: str = typer.Option(
        "",
        "--description",
        help="One-line description; passed to gh repo create + Copier. Default: 'A mini-app'.",
    ),
    variant: str = typer.Option(
        "full",
        "--variant",
        help="Template variant: full | lightweight-api | static-dashboard.",
    ),
    deploy_target: DeployTarget | None = typer.Option(
        None,
        "--deploy-target",
        case_sensitive=False,
        help=(
            "Where the app deploys: render (Blueprint in render.yaml) or "
            "railway. Default: railway when --no-railway or "
            "--railway-workspace is given on the command line, else render."
        ),
    ),
    auth_provider: AuthProvider | None = typer.Option(
        None,
        "--auth-provider",
        case_sensitive=False,
        help="Sign-in provider: google (default) or logto.",
    ),
    no_deploy: bool = typer.Option(
        False,
        "--no-deploy",
        help=(
            "Skip the deploy provider. Render target: no Render CLI calls; "
            "render.yaml is still rendered. Railway target: same as --no-railway."
        ),
    ),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        help=(
            "Parent directory for the generated app (the app goes in "
            "<output-dir>/<slug>). Default: the folder that holds the agentic-core checkout."
        ),
    ),
    railway_workspace: str | None = typer.Option(
        None,
        "--railway-workspace",
        envvar="RAILWAY_WORKSPACE",
        help=(
            "Railway workspace ID or name; forwarded to `railway init -w`. "
            "Given on the command line, it selects the Railway target. "
            "Defaults to RAILWAY_WORKSPACE (including .env), which never "
            "selects a target. With neither, `railway init` uses your only "
            "workspace, or fails if you have more than one."
        ),
    ),
    platform_domain: str | None = typer.Option(
        None,
        "--platform-domain",
        envvar="PLATFORM_DOMAIN",
        help=(
            "Logto only: your domain for the API identifiers "
            "(https://<app>.<domain>/api). Defaults to PLATFORM_DOMAIN "
            "(including .env). Google sign-in does not use it."
        ),
    ),
    keystore_path: str | None = typer.Option(
        None,
        "--admin-keystore",
        help=(
            "Override the admin keystore path. R7 precedence: this flag > "
            "AGENTIC_CORE_ADMIN_KEYSTORE env var > "
            "~/.agentic-core/admin.keystore.json."
        ),
    ),
    passphrase_fd: int | None = typer.Option(
        None,
        "--passphrase-fd",
        help=(
            "File descriptor to read the keystore passphrase from (R1, "
            "for agents and CI). Default: getpass prompt."
        ),
    ),
    no_auth: bool = typer.Option(
        False, "--no-auth", help="Skip all Logto provisioning (R11)."
    ),
    no_db: bool = typer.Option(
        False, "--no-db", help="Skip Postgres provisioning (R11)."
    ),
    no_frontend: bool = typer.Option(
        False,
        "--no-frontend",
        help="Skip React/RN frontend scaffold (`full` variant only) (R11).",
    ),
    no_mobile: bool = typer.Option(
        False,
        "--no-mobile",
        help="Skip Expo mobile scaffold (`full` variant only) (R11).",
    ),
    no_railway: bool = typer.Option(
        False, "--no-railway", help="Skip Railway provisioning (local dev) (R11)."
    ),
    no_github: bool = typer.Option(
        False, "--no-github", help="Skip GitHub repo creation (local dev) (R11)."
    ),
    with_auth: bool = typer.Option(
        False,
        "--with-auth",
        help="Opt-in Logto SPA for `static-dashboard` variant (R11).",
    ),
    with_db: bool = typer.Option(
        False,
        "--with-db",
        help="Opt-in Postgres for `static-dashboard` variant (R11).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Short-circuit before any external call; emit a synthetic success envelope.",
    ),
    preflight_only: bool = typer.Option(
        False,
        "--preflight",
        help=(
            "Run only credential/CLI/token/keystore preflight (§R6) and exit; "
            "create nothing. Exit 0 if all checks pass."
        ),
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Continue from `.bootstrap-state.json` checkpoint after a partial failure.",
    ),
    scaffolded_by: str = typer.Option(
        "manual",
        "--scaffolded-by",
        help="Provenance marker: `agent` (a coding agent) or `manual` (a person). §R18.",
    ),
    api_version: int = typer.Option(
        CURRENT_SCHEMA_VERSION,
        "--api-version",
        help=(
            f"Request a specific schema_version on the output envelope. "
            f"Today only {CURRENT_SCHEMA_VERSION} exists; passing anything "
            f"else exits 1 with a migration pointer (R12)."
        ),
    ),
    # Deprecated alias — caught explicitly to give a useful migration hint
    # rather than Click's generic "No such option" message.
    no_logto: bool = typer.Option(
        False,
        "--no-logto",
        hidden=True,
        help="DEPRECATED — use --no-auth (R11).",
    ),
) -> None:
    """Scaffold + provision + deploy a new mini-app end-to-end.

    Wraps :func:`agentic_core.bootstrap.orchestrator.orchestrate_create_app`
    with the R11 canonical flag set, R12 schema versioning, R4 exit-code
    contract, and R2 stdout discipline. Use ``--dry-run`` to validate the
    full plan without making any external calls.
    """
    json_mode = bool(ctx.obj and ctx.obj.get("json"))

    # ------------------------------------------------------------------
    # Flag validation (R11 + R12) — exit 1 on user errors before any work
    # ------------------------------------------------------------------
    if no_logto:
        # Migration aid: --no-logto was the old name; redirect to --no-auth.
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=variant,
            checkpoint="flag_validation",
            exit_code=1,
            error_code="DEPRECATED_FLAG",
            error_message=(
                "--no-logto has been removed. Use --no-auth instead. "
                "See docs/SECURITY.md or run `agentic-core create-app --help` "
                "for the current flag names."
            ),
            retryable=False,
        )
        raise typer.Exit(code=1)

    if no_auth and with_auth:
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=variant,
            checkpoint="flag_validation",
            exit_code=1,
            error_code="CONFLICTING_FLAGS",
            error_message=(
                "--no-auth and --with-auth are mutually exclusive (R11)."
            ),
            retryable=False,
        )
        raise typer.Exit(code=1)

    if no_db and with_db:
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=variant,
            checkpoint="flag_validation",
            exit_code=1,
            error_code="CONFLICTING_FLAGS",
            error_message=(
                "--no-db and --with-db are mutually exclusive (R11)."
            ),
            retryable=False,
        )
        raise typer.Exit(code=1)

    if api_version != CURRENT_SCHEMA_VERSION:
        # R12: bumping requires agentic-core and its agent callers to
        # coordinate; we don't emit multi-version shapes.
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=variant,
            checkpoint="flag_validation",
            exit_code=1,
            error_code="UNSUPPORTED_API_VERSION",
            error_message=(
                f"--api-version {api_version} not supported. Today only "
                f"version {CURRENT_SCHEMA_VERSION} exists. See "
                f"docs/SECURITY.md changelog for migration guidance."
            ),
            retryable=False,
        )
        raise typer.Exit(code=1)

    if scaffolded_by not in ("agent", "manual"):
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=variant,
            checkpoint="flag_validation",
            exit_code=1,
            error_code="INVALID_SCAFFOLDED_BY",
            error_message=(
                f"--scaffolded-by must be 'agent' or 'manual' (got {scaffolded_by!r})."
            ),
            retryable=False,
        )
        raise typer.Exit(code=1)

    # ------------------------------------------------------------------
    # KTD11: resolve the deploy target. An explicit --deploy-target wins; a
    # command-line --no-railway or --railway-workspace selects railway; a
    # --resume uses the target in the existing state file (KTD8), so the same
    # command line resumes a run that started on Railway; else render.
    # Conflicts with the Render target are bad-flag user errors (exit 1, R4).
    # ------------------------------------------------------------------
    railway_workspace_given = _from_command_line(ctx, "railway_workspace")
    recorded = orchestrator.recorded_bootstrap_state(slug, output_dir) if resume else None
    recorded_target = recorded.deploy_target if recorded else None
    if deploy_target is not None:
        resolved_target = deploy_target.value
    elif no_railway or railway_workspace_given:
        resolved_target = DeployTarget.railway.value
    elif recorded_target is not None:
        resolved_target = recorded_target
    else:
        resolved_target = DeployTarget.render.value

    if resolved_target == DeployTarget.render.value:
        conflicts = [
            flag
            for flag, given in (
                ("--no-railway", no_railway),
                ("--railway-workspace", railway_workspace_given),
                ("--with-auth", with_auth),
            )
            if given
        ]
        if conflicts:
            _emit_failure(
                json_mode=json_mode,
                app_slug=slug,
                variant=variant,
                checkpoint="flag_validation",
                exit_code=1,
                error_code="DEPLOY_TARGET_CONFLICT",
                error_message=(
                    f"{', '.join(conflicts)} cannot be used with the Render "
                    "deploy target. Use --deploy-target railway for these flags."
                ),
                retryable=False,
            )
            raise typer.Exit(code=1)
    is_render = resolved_target == DeployTarget.render.value

    if auth_provider is not None:
        selected_auth = auth_provider.value
    elif recorded:
        selected_auth = recorded.resolved_auth_provider
    elif deploy_target is None and (no_railway or railway_workspace_given):
        # Keep existing agent scripts working until they pass both flags.
        selected_auth = "logto"
    else:
        selected_auth = "google"

    if with_auth and selected_auth != "logto":
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=variant,
            checkpoint="flag_validation",
            exit_code=1,
            error_code="AUTH_PROVIDER_CONFLICT",
            error_message="--with-auth needs --auth-provider logto. Google needs a backend.",
            retryable=False,
        )
        raise typer.Exit(code=1)

    # ------------------------------------------------------------------
    # Translate R11 flags → orchestrator booleans
    # ------------------------------------------------------------------
    # For static-dashboard, with_auth is the explicit opt-in. For other
    # variants, --no-auth is the canonical opt-out and with_auth is always
    # effectively True. The orchestrator's _variant_to_logto_flags handles
    # the matrix; we just translate the CLI surface into its inputs.
    if variant == "static-dashboard":
        resolved_with_auth = with_auth and not no_auth
    else:
        resolved_with_auth = not no_auth

    resolved_with_db = (with_db or variant != "static-dashboard") and not no_db
    resolved_with_frontend = not no_frontend
    resolved_with_mobile = not no_mobile
    # The Render target skips the keystore and Logto (KTD11).
    resolved_with_logto = resolved_with_auth and selected_auth == "logto" and not is_render
    resolved_with_railway = not no_railway and not no_deploy and not is_render
    resolved_with_deploy = not no_deploy
    resolved_with_github = not no_github

    # Populate os.environ from ./.env + ~/.agentic-apps.env before preflight,
    # which reads RAILWAY_API_TOKEN straight off the environment. Without this
    # a bare `agentic-core create-app` fails PREFLIGHT_ENV_MISSING even though
    # the token is sitting in .env. Exported shell vars still win.
    _load_env_files()

    # Typer resolves option envvars before entering this callback, while the
    # repo's .env files are intentionally loaded inside the callback. Re-read
    # RAILWAY_WORKSPACE now so a value stored in .env actually takes effect.
    # There is no built-in workspace: an account with several workspaces
    # must name one, or non-interactive `railway init` fails.
    if not railway_workspace:
        railway_workspace = os.environ.get("RAILWAY_WORKSPACE") or None
    # Same for PLATFORM_DOMAIN (Logto API identifiers; no built-in domain).
    if not platform_domain:
        platform_domain = os.environ.get("PLATFORM_DOMAIN") or None

    # ------------------------------------------------------------------
    # --preflight: validate creds/CLIs/tokens/keystore only, then exit (§R6)
    # ------------------------------------------------------------------
    if preflight_only:
        pf = preflight.run_preflight(
            deploy_target=resolved_target,
            with_deploy=resolved_with_deploy,
            with_railway=resolved_with_railway,
            with_github=resolved_with_github,
            with_logto=resolved_with_logto,
            keystore_path=Path(keystore_path) if keystore_path else None,
            platform_domain=platform_domain,
        )
        if pf.get("status") == "failed":
            # Fix 10: a preflight failure is STRUCTURAL — a missing binary, a
            # logged-out CLI, an absent token/keystore. Re-running the exact
            # same command cannot fix it, so mirror the orchestrator's
            # retryable=False for the same failure classes (the full create-app
            # path reports False; the standalone --preflight path must match).
            _emit_failure(
                json_mode=json_mode,
                app_slug=slug,
                variant=variant,
                checkpoint="preflight",
                exit_code=pf.get("exit_code", 5),
                error_code=pf.get("error_code", "PREFLIGHT_FAILED"),
                error_message=pf.get("error_message", "preflight validation failed"),
                retryable=False,
            )
            raise typer.Exit(code=pf.get("exit_code", 5))
        if json_mode:
            output.render_json({"schema_version": CURRENT_SCHEMA_VERSION, **pf})
        else:
            output.console.print("[green]Preflight passed[/green] — all credentials present.")
        raise typer.Exit(code=0)

    # ------------------------------------------------------------------
    # Invoke orchestrator under a top-level exception net (R2/R3/R4)
    # ------------------------------------------------------------------
    try:
        result_dict: dict[str, Any] = orchestrator.orchestrate_create_app(
            slug,
            variant=variant,
            description=description,
            with_auth=resolved_with_auth,
            with_db=resolved_with_db,
            with_frontend=resolved_with_frontend,
            with_mobile=resolved_with_mobile,
            with_railway=resolved_with_railway,
            with_logto=resolved_with_logto,
            with_github=resolved_with_github,
            railway_workspace=railway_workspace,
            keystore_path=Path(keystore_path) if keystore_path else None,
            passphrase_fd=passphrase_fd,
            platform_domain=platform_domain,
            resume=resume,
            dry_run=dry_run,
            scaffolded_by=scaffolded_by,  # type: ignore[arg-type]
            output_dir=output_dir,
            raise_on_failure=True,
            progress=(None if json_mode else output.console.print),
            deploy_target=resolved_target,  # type: ignore[arg-type]
            auth_provider=selected_auth,
            with_deploy=resolved_with_deploy,
        )
    except SystemExit as exc:
        # The orchestrator stashes the §R3 failure envelope on .envelope and
        # raises SystemExit(code). Surface it through our JSON-or-pretty
        # rendering rather than letting Typer print its own message.
        envelope = getattr(exc, "envelope", None)
        if envelope is not None:
            if json_mode:
                # Validate through FailureEnvelope to lock the shape — drift
                # in the orchestrator's dict would fail loudly here.
                try:
                    fe = FailureEnvelope.model_validate(envelope)
                    output.render_json(fe.model_dump(mode="json"))
                except ValidationError:
                    # Shape drift: emit a best-effort envelope rather than
                    # corrupting stdout with raw orchestrator output.
                    output.render_json(envelope)
            else:
                output.console.print(
                    f"[red]Error[/red] ({envelope.get('error_code')}, "
                    f"exit {envelope.get('exit_code')}): "
                    f"{envelope.get('error_message')}"
                )
        # Re-raise with the same exit code so the shell sees it.
        code = exc.code if isinstance(exc.code, int) else 1
        raise typer.Exit(code=code) from exc
    except Exception as exc:  # noqa: BLE001 — final safety net
        # R2: no Python traceback on stdout under any circumstance. Any
        # unhandled exception → §R3 envelope with exit 3 (provisioning class)
        # and a clear error_code so the caller knows it's unexpected.
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=variant,
            checkpoint="orchestrator_unhandled",
            exit_code=3,
            error_code="UNHANDLED_EXCEPTION",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise typer.Exit(code=3) from exc

    # ------------------------------------------------------------------
    # Success: render via SuccessEnvelope (free schema validation)
    # ------------------------------------------------------------------
    try:
        envelope_model = SuccessEnvelope.model_validate(result_dict)
    except ValidationError as exc:
        # Shape drift between orchestrator and schema = a real bug; surface
        # it as an UNHANDLED_EXCEPTION rather than emit garbage.
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=variant,
            checkpoint="envelope_validation",
            exit_code=3,
            error_code="ENVELOPE_VALIDATION_FAILED",
            error_message=str(exc),
            retryable=False,  # shape drift is a code bug, not transient
        )
        raise typer.Exit(code=3) from exc

    if json_mode:
        output.render_json(envelope_model.model_dump(mode="json"))
    else:
        # Pretty mode: a short summary on stderr (R2 keeps stdout clean
        # even in pretty mode so `agentic-core create-app foo | jq .` would
        # work were one ever tempted to do it).
        output.console.print(
            f"[green]Scaffolded[/green] [bold]{envelope_model.app_slug}[/bold] "
            f"(variant={envelope_model.variant}, "
            f"dry_run={envelope_model.dry_run})"
        )
        if envelope_model.auth and envelope_model.auth.get("next_step"):
            output.console.print(
                f"[yellow]![/yellow] {envelope_model.auth['next_step']}",
                soft_wrap=True,
            )
        if envelope_model.render:
            blueprint_url = envelope_model.render.get("blueprint_url")
            if blueprint_url:
                output.console.print(
                    f"[cyan]→[/cyan] Apply the Render Blueprint: {blueprint_url}",
                    soft_wrap=True,
                )
            elif envelope_model.render.get("next_step"):
                output.console.print(
                    f"[cyan]→[/cyan] Next step: {envelope_model.render['next_step']}",
                    soft_wrap=True,
                )


# ---------------------------------------------------------------------------
# list-apps — Task #10 (§R18 registry-only)
# ---------------------------------------------------------------------------


@app.command("list-apps")
def list_apps_cmd(
    ctx: typer.Context,
    root_dir: str | None = typer.Option(
        None,
        "--root-dir",
        help=(
            "Override the directory scanned for sibling apps. Defaults to the "
            "parent of the agentic-core checkout. Useful for tests and "
            "agent-sandbox setups that keep apps in a non-standard location."
        ),
    ),
) -> None:
    """List sibling apps from on-disk ``.bootstrap-state.json`` files (§R18).

    Pure registry view — does NOT call Logto, GitHub, or Railway to verify
    resources still exist. Agents use this for fast inventory without burning
    API quota. Operators (or a future ``agentic-core verify``) own live checks.

    Always emits JSON on stdout (machine-only today). The root ``--json`` flag
    is accepted but redundant; there is no pretty mode for ``list-apps``.
    """
    # ``json_mode`` is unused — list-apps is JSON-only today. We still honor
    # ``--json`` on the root callback (it's a no-op) so the surface is uniform
    # with sibling subcommands and so callers building ``agentic-core --json X``
    # wrappers don't have to special-case this command.
    del ctx
    override = Path(root_dir).expanduser() if root_dir else None
    envelope = list_apps.scan_registry(override)
    output.render_json(envelope.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# teardown — U8 (R7 / KTD5) manual escape hatch
# ---------------------------------------------------------------------------


@app.command("teardown")
def teardown_cmd(
    ctx: typer.Context,
    slug: str = typer.Argument(..., help="App slug to tear down."),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the interactive confirmation prompt (non-interactive teardown).",
    ),
    root_dir: str | None = typer.Option(
        None,
        "--root-dir",
        help=(
            "Override the directory scanned for the app's "
            "``.bootstrap-state.json``. Defaults to the parent of the "
            "agentic-core checkout."
        ),
    ),
) -> None:
    """Delete the per-app Logto + Railway resources a bootstrap run created.

    Reads the app's ``.bootstrap-state.json`` and deletes its Logto SPA/M2M
    apps, API resource, user, and its Railway project. NEVER deletes the
    GitHub repo, and NEVER targets the shared ``agentic-core-admin`` app
    (KTD5). ALWAYS confirms first unless ``--yes`` is passed.
    """
    json_mode = bool(ctx.obj and ctx.obj.get("json"))

    # Fix 2 — validate the slug BEFORE the confirmation prompt (and before it
    # reaches teardown_app / any Path join). teardown is destructive and --yes
    # skips the confirmation that would otherwise reveal a traversal slug, so a
    # bad slug must be a clean exit-1 user error here.
    if not teardown.SLUG_REGEX.match(slug):
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=None,
            checkpoint="slug_validation",
            exit_code=1,
            error_code="INVALID_SLUG",
            error_message=(
                f"slug must match {teardown.SLUG_REGEX.pattern} (got {slug!r})"
            ),
            retryable=False,
        )
        raise typer.Exit(code=1)

    # INVARIANT 3 — confirmation unless --yes.
    #
    # Fix 11: in --json (machine / non-interactive) mode we must NOT call
    # ``typer.confirm``. A prompt would print to stdout and a declined/aborted
    # prompt would emit a bare "Aborted." to stderr with NO JSON envelope,
    # leaving an agent with unparseable output. Instead emit a proper
    # CONFIRMATION_REQUIRED failure envelope (deterministic → retryable=False)
    # telling the caller to pass --yes, and exit non-zero. Interactive
    # (non-json) callers keep the ``typer.confirm`` prompt, which ``abort=True``
    # turns into a clean exit-1 abort on a declined prompt.
    if not yes:
        if json_mode:
            _emit_failure(
                json_mode=True,
                app_slug=slug,
                variant=None,
                checkpoint="confirmation",
                exit_code=1,
                error_code="CONFIRMATION_REQUIRED",
                error_message=(
                    "Refusing to tear down without confirmation in --json / "
                    "non-interactive mode. Re-run with --yes to confirm "
                    f"deletion of ALL Logto + Railway resources for {slug!r}."
                ),
                retryable=False,
            )
            raise typer.Exit(code=1)
        typer.confirm(
            f"Delete ALL Logto + Railway resources for {slug!r}? "
            "This cannot be undone.",
            abort=True,
        )

    # Decrypt the admin keystore to get a LIVE management credential. Teardown
    # deletes Logto resources, which needs admin auth; relying on the on-disk
    # management-token cache fails once it expires (~1h) — the common case for
    # a manual teardown run some time after the bootstrap. Decrypt like
    # create-app does (getpass prompt unless --passphrase-fd), so teardown works
    # standalone regardless of cache state.
    # Read ./.env and ~/.agentic-apps.env like create-app does: teardown needs
    # LOGTO_ENDPOINT and LOGTO_M2M_APP_ID from there. Exported values win.
    _load_env_files()
    admin_credential: bytearray | None = None
    try:
        _ks_path = keystore.resolve_keystore_path(None)
        _ks_env = keystore.load_keystore(_ks_path)
        _passphrase = keystore.read_passphrase(None)
        admin_credential = keystore.decrypt_credential(_ks_env, _passphrase)
    except keystore.KeystoreNotFound:
        # No keystore — fall back to cache-only (may still work if warm).
        admin_credential = None
    except keystore.KeystoreDecryptionError:
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=None,
            checkpoint="keystore",
            exit_code=2,
            error_code="KEYSTORE_DECRYPT_FAILED",
            error_message="Admin keystore decryption failed (wrong passphrase?).",
            retryable=False,
        )
        raise typer.Exit(code=2) from None

    try:
        summary = teardown.teardown_app(
            slug,
            assume_yes=True,  # confirmation already handled above (or --yes)
            root=Path(root_dir).expanduser() if root_dir else None,
            admin_credential=admin_credential,
        )
    except FileNotFoundError:
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=None,
            checkpoint="teardown",
            exit_code=1,
            error_code="STATE_NOT_FOUND",
            error_message=(
                f"No .bootstrap-state.json found for {slug!r}. Nothing to tear down."
            ),
            retryable=False,  # deterministic — the file won't appear on a retry
        )
        raise typer.Exit(code=1) from None
    except Exception as exc:  # noqa: BLE001 — final safety net (R2/R3)
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=None,
            checkpoint="teardown",
            exit_code=3,
            error_code="TEARDOWN_FAILED",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        raise typer.Exit(code=3) from exc

    # Finding 2b — a teardown that failed (a delete raised, a railway delete
    # returned failed, or a guard refused) reports status "failed". The CLI must
    # exit NON-ZERO so --json and human callers both see it as a failure instead
    # of a false success. A clean success or a declined-confirmation abort stays
    # status "success" → exit 0.
    if summary.get("status") == "failed":
        td = summary.get("teardown", {})
        reason = td.get("aborted_reason")
        failed = td.get("failed") or []
        # Fix 22 — classify by failure kind rather than collapsing everything to
        # exit 3. A guard refusal (admin-guard-unresolved / slug-mismatch) sets
        # ``aborted_reason`` and is DETERMINISTIC: nothing was deleted, the
        # operator must fix config and re-run — a user error (exit 1,
        # retryable=False). A genuine delete failure (``failed`` non-empty, no
        # reason) is a provisioning-class error (exit 3) that a retry may clear
        # (retryable=True).
        if reason:
            error_code = "TEARDOWN_REFUSED"
            exit_code = 1
            retryable = False
            message = reason
        else:
            error_code = "TEARDOWN_FAILED"
            exit_code = 3
            retryable = True
            message = f"teardown failed to delete: {', '.join(failed) or 'unknown'}"
        # Fix 23 — preserve the rich per-resource summary on the failure path so
        # an operator/agent sees exactly what was and wasn't deleted, not just a
        # bare error envelope.
        _emit_failure(
            json_mode=json_mode,
            app_slug=slug,
            variant=None,
            checkpoint="teardown",
            exit_code=exit_code,
            error_code=error_code,
            error_message=message,
            retryable=retryable,
            extra={"teardown": td} if td else None,
        )
        raise typer.Exit(code=exit_code)

    if json_mode:
        output.render_json(summary)
    else:
        td = summary.get("teardown", {})
        output.console.print(
            f"[green]Teardown[/green] [bold]{slug}[/bold] — "
            f"deleted={td.get('deleted')}, "
            f"already_absent={td.get('already_absent')}, "
            f"skipped_admin={td.get('skipped_admin')}, "
            f"preserved={td.get('preserved')}"
        )


if __name__ == "__main__":
    app()
