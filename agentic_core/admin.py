"""``agentic-core admin`` subcommand group.

One-time admin operations for operators. Today this hosts a single
subcommand:

  - ``agentic-core admin init-keystore`` — write the encrypted
    ``agentic-core-admin`` keystore from a pasted M2M id + secret, then
    verify the round-trip by minting a Logto management token with the
    decrypted credential.

R20 two-phase UX:

  1. Read ``LOGTO_M2M_APP_ID`` and ``LOGTO_M2M_APP_SECRET`` from the
     process env if present; otherwise read them interactively (id via
     ``typer.prompt``, secret via ``getpass``). The secret is **never**
     accepted as a CLI flag because flags land in ``argv`` and leak via
     ``ps``, shell history, and ``/proc``.
  2. Read the keystore passphrase via ``--passphrase-fd N`` (agent
     path) or ``getpass`` (interactive operator path). Confirm by reprompting.
  3. Summarize what will be encrypted (id + target path, NEVER the secret).
     Ask ``typer.confirm`` unless ``--yes`` is passed.
  4. Write the encrypted envelope atomically with mode 0600 via
     :func:`agentic_core.bootstrap.keystore.write_keystore_atomic`.
  5. Verify locally: re-read the file, decrypt with the same passphrase,
     assert byte-equality with the original secret. Catches any silent
     write or encryption bug *before* we tell the operator it's safe to
     delete their env vars.
  6. Verify against Logto: build a :class:`LogtoManagementClient` with the
     decrypted credential and mint a management token. Surfaces "the
     secret was wrong" / "the endpoint is unreachable" *before* anyone
     deletes their only copy.
  7. Print operator instruction to ``unset LOGTO_M2M_APP_SECRET`` from
     the parent shell and delete it from ``.env`` (we cannot do this from a
     child process). ``LOGTO_M2M_APP_ID`` stays: preflight still needs it.

Stdout discipline (R2):

  - In pretty mode: all human output is Rich on the stderr-bound
    :data:`agentic_core.output.console`.
  - In ``--json`` mode: the final stdout line is exactly one JSON object
    describing success / failure. All Rich output still goes to stderr.

Secret hygiene:

  - The M2M secret is held as a ``bytes`` object only as long as needed
    to encrypt + verify; we wrap it in a ``bytearray`` for the Logto
    verify pass so we can zero it after. The original ``bytes`` value
    cannot be zeroed (CPython immutability) — documented residual risk
    in ``docs/SECURITY.md``.
  - The Rich summary and the JSON success envelope deliberately omit
    the secret. A parameterized test in
    ``tests/unit/test_admin_init_keystore.py`` greps both streams for
    the literal secret value and fails the build if it appears.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Any

import typer

from agentic_core import output
from agentic_core.bootstrap import keystore, logto


# Env vars read for the M2M credential. Defined as constants so the
# operator-facing "now unset these" instruction stays in sync with the
# read sites. The keystore passphrase is intentionally NOT here — per R1
# we only read it from stdin or --passphrase-fd, never from env.
ENV_M2M_APP_ID = "LOGTO_M2M_APP_ID"
ENV_M2M_APP_SECRET = "LOGTO_M2M_APP_SECRET"
ENV_LOGTO_ENDPOINT = "LOGTO_ENDPOINT"


admin_app: typer.Typer = typer.Typer(
    name="admin",
    help=(
        "One-time admin operations for operators. "
        "Today: init-keystore for the Logto Management API credential."
    ),
    no_args_is_help=True,
)


def _resolve_keystore_path(override: str | None) -> Path:
    """Resolve the target keystore path using the R7 precedence rules.

    Wraps :func:`keystore.resolve_keystore_path` so tests can monkeypatch
    one site if needed.
    """
    return keystore.resolve_keystore_path(override)


def _read_passphrase_twice_with_confirm(fd: int | None) -> str:
    """Read the passphrase, then confirm by re-reading.

    If ``fd`` is supplied (agent or CI pipe path), we read it ONCE and
    skip confirmation — the caller is a programmatic invocation, not a
    human typo-prone keyboard. If ``fd`` is ``None``, prompt twice via
    ``getpass`` and require an exact match.
    """
    if fd is not None:
        return keystore.read_passphrase(fd)
    pw1 = getpass.getpass("New keystore passphrase: ")
    pw2 = getpass.getpass("Confirm passphrase: ")
    if pw1 != pw2:
        raise typer.BadParameter("passphrases did not match")
    if not pw1:
        raise typer.BadParameter("passphrase must not be empty")
    return pw1


def _read_m2m_credential(
    m2m_id_flag: str | None,
) -> tuple[str, bytes]:
    """Resolve the M2M id + secret pair.

    Priority for each value:

      1. Explicit ``--m2m-id`` flag (id only — the secret has NO flag form
         because flags leak via argv / ps / shell history).
      2. ``LOGTO_M2M_APP_ID`` / ``LOGTO_M2M_APP_SECRET`` env vars.
      3. Interactive prompt (id via ``typer.prompt``, secret via
         ``getpass``).

    Returns ``(app_id, secret_bytes)``. Raises ``typer.BadParameter`` if
    either value cannot be obtained.
    """
    app_id = m2m_id_flag or os.environ.get(ENV_M2M_APP_ID)
    if not app_id:
        try:
            app_id = typer.prompt("Logto M2M App ID")
        except (EOFError, OSError) as exc:
            raise typer.BadParameter(
                f"missing M2M App ID; set ${ENV_M2M_APP_ID} or pass --m2m-id"
            ) from exc
    app_id = app_id.strip()
    if not app_id:
        raise typer.BadParameter(f"M2M App ID is required (set ${ENV_M2M_APP_ID})")

    secret_str = os.environ.get(ENV_M2M_APP_SECRET)
    if not secret_str:
        try:
            secret_str = getpass.getpass("Logto M2M App Secret: ")
        except (EOFError, OSError) as exc:
            raise typer.BadParameter(
                f"missing M2M App Secret; set ${ENV_M2M_APP_SECRET}"
            ) from exc
    if not secret_str:
        raise typer.BadParameter(
            f"M2M App Secret is required (set ${ENV_M2M_APP_SECRET})"
        )

    return app_id, secret_str.encode("utf-8")


def _confirm_or_abort(*, app_id: str, target_path: Path, yes: bool) -> None:
    """R20 step 3: show a non-secret summary and ask for confirmation.

    Prints to stderr (R2 stdout discipline). If ``yes`` is True, skips
    the prompt — used by automated invocations (CI, agents).
    """
    output.console.print(
        f"[bold]Will write keystore[/bold] for app_id=[cyan]{app_id}[/cyan]"
    )
    output.console.print(f"  → [cyan]{target_path}[/cyan]")
    output.console.print(
        "  [dim](secret omitted from this summary by design)[/dim]"
    )
    if yes:
        return
    try:
        if not typer.confirm("Proceed?", default=False):
            raise typer.Abort()
    except typer.Abort:
        output.console.print("[yellow]Aborted by operator.[/yellow]")
        raise


def _verify_round_trip(target_path: Path, passphrase: str, original: bytes) -> None:
    """R20 step 5: re-read, decrypt, assert byte-equality with original."""
    envelope = keystore.load_keystore(target_path)
    out = keystore.decrypt_credential(envelope, passphrase)
    try:
        if bytes(out) != original:
            raise RuntimeError(
                "Keystore round-trip verification failed: decrypted bytes "
                "do not match the original secret. Refusing to declare "
                "success — investigate before deleting your env vars."
            )
    finally:
        # Zero the decrypted buffer regardless of success — we copied it
        # for the comparison above.
        out[:] = b"\x00" * len(out)


def _verify_logto_call(
    *, endpoint: str, app_id: str, secret: bytes
) -> None:
    """R20 step 6: actually mint a management token with the decrypted
    credential. Surfaces wrong-secret / unreachable-endpoint before the
    operator deletes their only copy of the secret.

    Errors are caught and re-raised as a typed ``RuntimeError`` whose
    message NEVER contains the secret bytes (the keystore client doesn't
    log the secret, but defense in depth: we catch the broad surface and
    re-message it).
    """
    cred = bytearray(secret)
    try:
        client = logto.LogtoManagementClient(
            endpoint=endpoint,
            admin_app_id=app_id,
            admin_credential=cred,
        )
        # Force a fresh mint (don't trust a stale cache hit to claim success).
        token, _expires_at = client._mint_management_token()
        if not token:
            raise RuntimeError(
                "Logto returned an empty access_token; verify failed."
            )
    except logto.LogtoAuthError as exc:
        # The exception text is keystore-friendly; safe to surface.
        raise RuntimeError(
            f"Logto rejected the credential during verification: {exc}"
        ) from exc
    except logto.LogtoError as exc:
        raise RuntimeError(
            f"Logto management token mint failed during verification: {exc}"
        ) from exc
    finally:
        # Zero the verify-pass bytearray. The caller's `secret` bytes
        # cannot be zeroed (immutable); see SEC-006 caveat.
        cred[:] = b"\x00" * len(cred)


@admin_app.command("init-keystore")
def init_keystore(
    ctx: typer.Context,
    keystore_path: str = typer.Option(
        None,
        "--keystore-path",
        help=(
            "Override the destination path. Defaults follow R7: "
            "$AGENTIC_CORE_ADMIN_KEYSTORE → ~/.agentic-core/admin.keystore.json."
        ),
    ),
    m2m_id: str = typer.Option(
        None,
        "--m2m-id",
        help=(
            f"Logto Management API M2M App ID. Defaults to "
            f"${ENV_M2M_APP_ID} env var or interactive prompt."
        ),
    ),
    passphrase_fd: int = typer.Option(
        None,
        "--passphrase-fd",
        help=(
            "Read the keystore passphrase from this open file descriptor "
            "(for agents and CI). Default: interactive getpass prompt."
        ),
    ),
    endpoint: str = typer.Option(
        None,
        "--logto-endpoint",
        help=(
            f"Logto base URL for the post-write verification call. "
            f"Defaults to ${ENV_LOGTO_ENDPOINT} env var."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the y/N confirm prompt (for automation / CI).",
    ),
) -> None:
    """Write the encrypted admin Logto Management credential keystore.

    Workflow: read id + secret (env or prompt), read passphrase, confirm,
    encrypt + atomic write 0600, re-decrypt verify, mint Logto token to
    verify the credential still works, print operator cleanup instructions.

    The M2M secret is NEVER accepted as a CLI flag and NEVER printed.
    """
    json_mode = bool(ctx.obj and ctx.obj.get("json"))

    # Read ./.env and ~/.agentic-apps.env like create-app does, so the README
    # setup (values in .env) works. Exported shell values still win.
    from agentic_core.cli import _load_env_files

    _load_env_files()

    target_path = _resolve_keystore_path(keystore_path)

    # ------------------------------------------------------------------
    # Phase 1: gather inputs
    # ------------------------------------------------------------------
    try:
        # Check the endpoint first, so a missing setting fails before any
        # secret is read. There is no built-in Logto endpoint.
        logto_endpoint = (endpoint or "").strip() or os.environ.get(
            ENV_LOGTO_ENDPOINT, ""
        ).strip()
        if not logto_endpoint:
            raise typer.BadParameter(
                f"Set {ENV_LOGTO_ENDPOINT} in .env, or pass --logto-endpoint."
            )
        app_id, secret_bytes = _read_m2m_credential(m2m_id)
        passphrase = _read_passphrase_twice_with_confirm(passphrase_fd)
    except typer.BadParameter as exc:
        if json_mode:
            output.emit_failure(
                checkpoint="gather_inputs",
                exit_code=5,
                error_code="MISCONFIG",
                error_message=str(exc),
            )
        else:
            output.console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=5) from None

    # ------------------------------------------------------------------
    # Phase 2: confirm (operator review of non-secret summary)
    # ------------------------------------------------------------------
    try:
        _confirm_or_abort(app_id=app_id, target_path=target_path, yes=yes)
    except typer.Abort:
        if json_mode:
            output.emit_failure(
                checkpoint="confirm",
                exit_code=1,
                error_code="ABORTED_BY_OPERATOR",
                error_message="operator declined the confirmation prompt",
            )
        raise typer.Exit(code=1) from None

    # ------------------------------------------------------------------
    # Phase 3: encrypt + write
    # ------------------------------------------------------------------
    try:
        envelope = keystore.encrypt_credential(secret_bytes, passphrase)
        keystore.write_keystore_atomic(target_path, envelope)
    except Exception as exc:  # noqa: BLE001 — fail-closed on any encryption error
        # Sanitize: drop any potential secret leakage from the exception text.
        # encrypt_credential doesn't reference the secret in its messages,
        # but a TypeError from a misuse upstream could include the value.
        msg = str(exc)
        if secret_bytes.decode("utf-8", errors="replace") in msg:
            msg = "encryption failed (secret redacted from error message)"
        if json_mode:
            output.emit_failure(
                checkpoint="encrypt_and_write",
                exit_code=3,
                error_code="ENCRYPT_OR_WRITE_FAILED",
                error_message=msg,
            )
        else:
            output.console.print(f"[red]Error:[/red] {msg}")
        raise typer.Exit(code=3) from None

    # ------------------------------------------------------------------
    # Phase 4: verify round-trip decrypt
    # ------------------------------------------------------------------
    try:
        _verify_round_trip(target_path, passphrase, secret_bytes)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if secret_bytes.decode("utf-8", errors="replace") in msg:
            msg = "round-trip verification failed (secret redacted)"
        if json_mode:
            output.emit_failure(
                checkpoint="verify_round_trip",
                exit_code=3,
                error_code="ROUND_TRIP_FAILED",
                error_message=msg,
            )
        else:
            output.console.print(f"[red]Error:[/red] {msg}")
        raise typer.Exit(code=3) from None

    # ------------------------------------------------------------------
    # Phase 5: verify against live Logto
    # ------------------------------------------------------------------
    try:
        _verify_logto_call(
            endpoint=logto_endpoint, app_id=app_id, secret=secret_bytes
        )
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if secret_bytes.decode("utf-8", errors="replace") in msg:
            msg = "Logto verification call failed (secret redacted)"
        if json_mode:
            output.emit_failure(
                checkpoint="verify_logto",
                exit_code=2,
                error_code="LOGTO_VERIFY_FAILED",
                error_message=msg,
            )
        else:
            output.console.print(f"[red]Error:[/red] {msg}")
        raise typer.Exit(code=2) from None

    # ------------------------------------------------------------------
    # Phase 6: success — print cleanup instructions
    # ------------------------------------------------------------------
    output.console.print(
        f"[green]Keystore written and verified[/green] at [cyan]{target_path}[/cyan]"
    )
    output.console.print(
        f"Now delete {ENV_M2M_APP_SECRET} from .env and unset it in your "
        "parent shell — the keystore holds it now:"
    )
    output.console.print(f"  [bold]unset {ENV_M2M_APP_SECRET}[/bold]")
    output.console.print(
        f"Keep {ENV_M2M_APP_ID}: it is not secret, and create-app still needs it."
    )

    if json_mode:
        # Success envelope — deliberately omits the secret.
        payload: dict[str, Any] = {
            "schema_version": 1,
            "status": "success",
            "keystore_path": str(target_path),
            "app_id": app_id,
            "logto_endpoint": logto_endpoint,
        }
        output.render_json(payload)
