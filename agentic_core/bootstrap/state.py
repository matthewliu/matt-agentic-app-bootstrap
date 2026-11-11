"""Bootstrap state file (``.bootstrap-state.json``) read/write.

This module is **security-critical** (spec §R8). It persists checkpoint
markers and resource IDs (Logto app IDs, GitHub repo URLs, Railway project
IDs) so that ``agentic-core create-app --resume`` can pick up where a
previous run left off. It MUST NOT persist secrets:

- M2M client secrets stay in process memory only.
- Admin keystore passphrases never touch disk in plaintext.
- The state file is written ``chmod 0600`` on every write.

The R8 entropy gate (test_bootstrap_state.py::TestEntropyGate) is the
mechanical guard: :func:`write_state` scans each ``resource_ids`` VALUE for a
32+ character contiguous ``[A-Za-z0-9+/=_]`` run and refuses the write if one
is found. Hyphen is deliberately excluded from the character class — it
segments standard formats (UUID v4 splits 8/4/4/4/12, ISO-8601 dates split
YYYY-MM-DD) so real Logto ulids and 36-char hyphenated Railway UUIDs break
into runs shorter than 32. Genuine secrets (base64 access tokens, M2M client
secrets) are dense hyphen-free strings and still trip the gate.

URL exemption (narrowed — Fix 14): ``/`` IS in the character class but does NOT
segment a run, so a legitimate repo URL like
``https://github.com/exampleowner/longexampleappname`` contains a 30+ char
contiguous run. A ``"://"`` value is therefore exempt from the dense-run check
— but ONLY when it is a PLAIN URL with no userinfo. A credential-bearing URL
(``scheme://user:pass@host``, e.g. a ``DATABASE_URL``) is refused outright: it
is the likeliest secret to reach ``resource_ids``, and the ``:`` in
``user:pass`` can segment the password below the 32-char threshold, so the
entropy check alone would miss it. A raw base64 secret has no ``://`` and still
trips the gate. The gate scans ``resource_ids`` values — the only sink for
externally-derived data — not the whole serialized payload.

Orchestrator contract:
    write_state(path, state)  # after every successful step
    read_state(path)          # at startup if --resume
    delete_state(path)        # after successful scaffold completion

On corrupt/truncated state, ``read_state`` raises :class:`StateFileCorrupt`
so the orchestrator fails closed rather than silently treating a partial
file as authoritative.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: R8 entropy gate (RUNTIME guard). Any 32+ char contiguous run of
#: ``[A-Za-z0-9+/=_]`` in a NON-URL ``resource_ids`` value is treated as a
#: possible secret. The same pattern (``ENTROPY_RE``) appears in
#: ``tests/unit/test_bootstrap_state.py``, but the runtime application differs:
#: :func:`write_state` scans ``resource_ids`` values individually and exempts
#: ``"://"``-shaped (URL) values, whereas the test also does a whole-payload
#: scan of the on-disk bytes. Hyphen is excluded so real UUIDs/ULIDs pass;
#: dense hyphen-free secrets (base64 tokens, M2M client secrets) trip it.
_ENTROPY_RE = re.compile(r"[A-Za-z0-9+/=_]{32,}")


def _url_has_userinfo(value: str) -> bool:
    """True if ``value`` is a URL whose authority carries a userinfo component.

    The authority is the substring between ``://`` and the next ``/``, ``?`` or
    ``#``; a userinfo component is anything before an ``@`` inside it. So
    ``postgres://user:pass@host/db`` → True, while ``https://github.com/a/b`` →
    False. Used by :func:`write_state` (Fix 14) to keep credential-bearing URLs
    out of the ``://`` entropy exemption.
    """
    after = value.split("://", 1)[1]
    authority = re.split(r"[/?#]", after, maxsplit=1)[0]
    return "@" in authority


class StateFileEntropyError(Exception):
    """Raised by :func:`write_state` when the serialized state contains a
    high-entropy (secret-shaped) run.

    This makes the R8 "no secrets on disk" invariant a MECHANICAL runtime
    guard rather than a test-only assertion: a future code path that tries to
    persist a secret into ``resource_ids`` fails fast BEFORE anything is
    written, instead of silently serializing the secret to ``0600`` disk.
    """


class StateFileCorrupt(Exception):
    """Raised when ``read_state`` cannot decode/validate the on-disk file.

    Distinct from :class:`FileNotFoundError` (which means "no prior run")
    so the orchestrator can tell the difference between a fresh start and a
    crashed-mid-write file requiring operator attention.
    """


class BootstrapState(BaseModel):
    """Persistent bootstrap state for a single ``create-app`` invocation.

    Fields:
        schema_version: Pinned at 1; bumping requires coordinated update of
            ``read_state`` to migrate.
        app_slug: The slug being scaffolded (``my-dashboard``).
        variant: One of ``full`` | ``lightweight-api`` | ``static-dashboard``.
            We don't ``Literal``-validate here so a future variant added by
            Task #15 doesn't break round-tripping older state files.
        scaffolded_by: §R18 — distinguishes manual runs from agent
            invocations (detected by orchestrator based on TTY / env var).
        created_at: Timestamp of state-file creation.
        checkpoints: Step-name → completed bool. The orchestrator consults
            this on ``--resume`` to skip steps already done.
        resource_ids: External resource IDs (Logto app IDs, GitHub repo
            URLs, Railway project IDs). NEVER secrets — only IDs.
        last_completed_step: Human-readable last successful checkpoint name.
            Used in the failure JSON envelope (spec §R3).
        last_failure_retryable: R8 resume gate. Records the ``retryable``
            classification of the *most recent* terminal failure. ``None`` on
            a state file that has only ever recorded successful checkpoints.
            When ``False``, the previous run failed with a *structural* error
            (verify auth 401, a redirect-URI PATCH that silently no-oped, an
            un-renderable template) that re-running cannot fix — so ``--resume``
            refuses rather than looping. A bool never trips the entropy gate.
        last_failure_code: The ``error_code`` of that most-recent failure
            (e.g. ``VERIFY_AUTH_REJECTED``), surfaced in the refuse message so
            the operator knows *why* resume declined. Short, hyphen-free-safe
            codes never approach the 32-char entropy threshold.
        deploy_target: KTD8. The deploy target this run started with
            (``railway`` or ``render``). State files written before this field
            existed load as ``railway``. A ``--resume`` with a different target
            fails non-retryable.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    app_slug: str
    variant: str
    scaffolded_by: Literal["agent", "manual"]
    created_at: datetime
    checkpoints: dict[str, bool] = Field(default_factory=dict)
    resource_ids: dict[str, str] = Field(default_factory=dict)
    last_completed_step: str | None = None
    last_failure_retryable: bool | None = None
    last_failure_code: str | None = None
    deploy_target: Literal["railway", "render"] = "railway"
    auth_provider: Literal["google", "logto"] | None = None

    @property
    def resolved_auth_provider(self) -> Literal["google", "logto"]:
        # Older runs selected sign-in from the deploy target.
        return self.auth_provider or ("google" if self.deploy_target == "render" else "logto")


def write_state(path: Path, state: BootstrapState) -> None:
    """Atomically write ``state`` to ``path`` with mode ``0600``.

    Uses the mkstemp + os.rename pattern so a crash mid-write
    cannot leave a truncated file alongside the old one.

    Idempotent w.r.t. mode: even if the file already existed with
    looser permissions, the post-rename ``chmod`` re-narrows to ``0600``.
    """
    # R8 runtime entropy gate — refuse fast if a secret-shaped run is present.
    # Done BEFORE touching the filesystem so a rejected write leaves nothing
    # behind.
    #
    # We scan the ``resource_ids`` VALUES individually rather than the whole
    # serialized payload: that dict is the only sink for externally-derived
    # values (Logto/GitHub/Railway API responses) and thus the only realistic
    # secret-injection vector. The other fields are internal, controlled, and
    # bool/short-code shaped (never secret-carrying).
    #
    # URL exemption (NARROWED — Fix 14): a value containing ``"://"`` is
    # URL-shaped and exempt from the dense-run check ONLY when it is a PLAIN URL
    # with no userinfo. This is load-bearing — ``/`` is IN the entropy class but
    # does NOT segment a run, so a legitimate repo URL like
    # ``https://github.com/exampleowner/longexampleappname`` contains a 30+ char
    # contiguous run (``com/exampleowner/...``) that a naive whole-payload scan
    # false-positives on, killing ALL state checkpointing for that app.
    #
    # BUT a credential-bearing URL — one with a userinfo component
    # (``scheme://user:pass@host``, e.g. a ``DATABASE_URL``) — is the likeliest
    # secret to ever land in ``resource_ids``, and the ``:`` in ``user:pass``
    # segments the password into a run that may fall under the 32-char
    # threshold, so the entropy check alone would not catch it. Such URLs are
    # therefore NOT exempt: they are refused outright. A raw base64 secret token
    # has no ``://`` and still trips the entropy gate below.
    for value in state.resource_ids.values():
        if "://" in value:
            if _url_has_userinfo(value):
                # Credential-bearing URL — never a resource ID. Do NOT echo the
                # value; it contains the secret.
                raise StateFileEntropyError(
                    f"refusing to write state file at {path}: a resource_ids "
                    "value is a credential-bearing URL (userinfo before '@', "
                    "e.g. a DATABASE_URL). State files must contain only "
                    "resource IDs, never secrets (§R8)."
                )
            continue  # plain URL — exempt (see comment above)
        match = _ENTROPY_RE.search(value)
        if match is not None:
            # Do NOT echo the matched substring — it may be the secret itself.
            raise StateFileEntropyError(
                f"refusing to write state file at {path}: found a "
                f"{len(match.group())}-char high-entropy run (possible secret). "
                "State files must contain only resource IDs, never secrets (§R8)."
            )
    payload = state.model_dump_json()

    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path_str = tempfile.mkstemp(
        prefix=".bootstrap-state.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_path_str)
    try:
        # Defense-in-depth: mkstemp creates 0600, but some umask configs or
        # filesystems may widen. Re-narrow before writing.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            # ``payload`` was serialized (and entropy-scanned) above; reuse it
            # so the scanned bytes are exactly the written bytes. We add a
            # newline at EOF for grep-friendliness.
            f.write(payload)
            f.write("\n")
        # Atomic rename within the same directory — overwrites any
        # pre-existing state file.
        os.rename(tmp_path, path)
        # Post-rename chmod: on some filesystems mkstemp's mode is lost
        # across rename. Belt-and-braces.
        os.chmod(path, 0o600)
    except Exception:
        # Best-effort cleanup of the tempfile if we never got to rename.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def read_state(path: Path) -> BootstrapState:
    """Load and validate a state file, or raise.

    Distinguishes:
        - file missing → :class:`FileNotFoundError` (caller treats as "no
          prior run", starts fresh).
        - file present but corrupt/truncated/invalid →
          :class:`StateFileCorrupt` (caller stops and surfaces a clear
          error; the operator needs to ``--reset`` or hand-fix).
    """
    # Let FileNotFoundError propagate — the orchestrator distinguishes
    # "fresh start" from "corrupt state" using this.
    raw = path.read_text()
    try:
        return BootstrapState.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        raise StateFileCorrupt(
            f"state file at {path} is unreadable: {exc}"
        ) from exc


def delete_state(path: Path) -> None:
    """Remove the state file after successful scaffold completion (§R8).

    Idempotent — missing file is a no-op (a successful no-resume run may
    never have written one).
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass
