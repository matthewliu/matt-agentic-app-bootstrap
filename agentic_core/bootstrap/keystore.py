"""Admin keystore: encrypted Logto Management credential at rest.

Encrypts/decrypts the ``agentic-core-admin`` Logto Management API credential
(``LOGTO_M2M_APP_ID`` + ``LOGTO_M2M_APP_SECRET``) using the eth-account v3
JSON envelope (scrypt + AES-128-CTR), a standard and widely used keystore
format. Used by ``agentic-core create-app`` to load
the admin credential at scaffold time and by ``agentic-core admin
init-keystore`` (task #7) to write it once.

Spec invariants enforced here:

- **R1 passphrase transport:** ``read_passphrase`` reads from either an
  interactive ``getpass`` stdin prompt or a caller-supplied file descriptor
  (``--passphrase-fd N``). It NEVER reads a passphrase env var — that path
  was removed because env vars are readable from ``/proc/<pid>/environ`` by
  any same-UID process for the scaffold duration. The project-level grep
  gate plus a pinned test forbid the env-var name from appearing anywhere
  in this module.

- **R7 keystore path precedence:** ``resolve_keystore_path`` returns, in
  order, ``--admin-keystore`` flag value > ``AGENTIC_CORE_ADMIN_KEYSTORE``
  env var > default (``~/.agentic-core/admin.keystore.json``).

- **SEC-006 secret hygiene:** ``decrypt_credential`` returns a
  ``bytearray`` rather than ``bytes`` so callers can zero the buffer with
  ``secret[:] = b'\\x00' * len(secret)`` after use. *Residual risk:* CPython
  may have made internal copies of the plaintext during decryption that we
  cannot reach to zero, and bytes-immutability means any intermediate
  ``bytes`` objects produced by ``decrypt_aes_ctr`` persist until garbage
  collection. We do the best we can; this is a known limitation documented
  in ``docs/SECURITY.md``.

- **Invariant 4 fail-closed:** missing keystore raises
  :class:`KeystoreNotFound` (operator-facing message includes the resolved
  path); bad passphrase raises :class:`KeystoreDecryptionError`. Neither
  falls back to env vars.

Why a hand-rolled v3 envelope instead of ``eth_account.Account.encrypt``?
``Account.encrypt`` hard-asserts a 32-byte plaintext (Ethereum private key
size). We need to encrypt variable-length credential JSON, so we reuse the
underlying ``eth_keyfile`` primitives directly (scrypt, AES-128-CTR, MAC).
The resulting envelope is v3-format-compatible *minus* the ``address``
field, which doesn't apply when there's no associated EVM key. Operators
reading the file will recognize the shape; the JSON is fully self-describing.
"""

from __future__ import annotations

import errno
import getpass
import json
import os
import tempfile
import uuid
from pathlib import Path

from Crypto.Random import get_random_bytes
from eth_keyfile.keyfile import (  # type: ignore[import-not-found]
    DKLEN,
    SCRYPT_P,
    SCRYPT_R,
    _scrypt_hash,
    decrypt_aes_ctr,
    encrypt_aes_ctr,
    get_default_work_factor_for_kdf,
)
from eth_utils import big_endian_to_int, decode_hex, encode_hex, int_to_big_endian, keccak

# ---------------------------------------------------------------------------
# Default paths (R7 — patchable in tests via monkeypatch).
# ---------------------------------------------------------------------------

#: Default location, outside the repo. On a shared server, pass
#: ``--admin-keystore`` or set ``AGENTIC_CORE_ADMIN_KEYSTORE`` instead.
LOCAL_KEYSTORE_PATH: Path = Path.home() / ".agentic-core" / "admin.keystore.json"

#: Env var that callers may set to point at a non-default keystore path.
#: NOTE: This is the *path* env var (where the file lives) — NOT a passphrase
#: env var. See R1: passphrase transport is stdin-or-fd only.
KEYSTORE_PATH_ENV_VAR = "AGENTIC_CORE_ADMIN_KEYSTORE"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class KeystoreError(Exception):
    """Base class for keystore-related failures (operator-facing)."""


class KeystoreNotFound(KeystoreError):
    """Raised when the resolved keystore path does not exist on disk.

    The error message is intentionally explicit (``keystore not found at
    <path>``) so operators can copy/paste it into a debug session.
    Critically, this raises *instead of* falling back to env-var-supplied
    credentials — see Invariant 4.
    """


class KeystoreDecryptionError(KeystoreError):
    """Raised when AES-CTR/MAC verification fails (wrong passphrase or
    tampered ciphertext).

    Message includes the literal phrase ``decryption failed`` so the
    bootstrap CLI's error renderer can pattern-match it for exit-code 2.
    """


class PassphraseReadError(KeystoreError):
    """Raised when reading the passphrase via fd or stdin fails."""


# ---------------------------------------------------------------------------
# encrypt_credential / decrypt_credential
# ---------------------------------------------------------------------------


def encrypt_credential(secret: bytes, passphrase: str) -> dict:
    """Wrap ``secret`` in an eth-account v3 JSON envelope.

    Uses scrypt for KDF (default work factor ``n=262144``, ``r=8``, ``p=1``)
    and AES-128-CTR for the cipher, matching ``eth_account``'s v3 layout.
    Each call produces a fresh 16-byte salt and 16-byte IV — encrypting the
    same plaintext twice yields different ciphertexts and IDs.

    Returns a JSON-serializable ``dict`` (the canonical eth-account v3 shape
    minus ``address``, which doesn't apply for non-EVM credentials).
    """
    if not isinstance(secret, (bytes, bytearray, memoryview)):
        raise TypeError(f"secret must be bytes-like, got {type(secret).__name__}")
    if not isinstance(passphrase, str):
        raise TypeError(f"passphrase must be str, got {type(passphrase).__name__}")

    salt = get_random_bytes(16)
    work_factor = get_default_work_factor_for_kdf("scrypt")
    derived_key = _scrypt_hash(
        passphrase,
        salt=salt,
        buflen=DKLEN,
        r=SCRYPT_R,
        p=SCRYPT_P,
        n=work_factor,
    )

    iv = big_endian_to_int(get_random_bytes(16))
    encrypt_key = derived_key[:16]
    ciphertext = encrypt_aes_ctr(bytes(secret), encrypt_key, iv)
    mac = keccak(derived_key[16:32] + ciphertext)

    return {
        "crypto": {
            "cipher": "aes-128-ctr",
            "cipherparams": {
                "iv": encode_hex(int_to_big_endian(iv))[2:],
            },
            "ciphertext": encode_hex(ciphertext)[2:],
            "kdf": "scrypt",
            "kdfparams": {
                "dklen": DKLEN,
                "n": work_factor,
                "r": SCRYPT_R,
                "p": SCRYPT_P,
                "salt": encode_hex(salt)[2:],
            },
            "mac": encode_hex(mac)[2:],
        },
        "id": str(uuid.uuid4()),
        "version": 3,
    }


def decrypt_credential(keystore_dict: dict, passphrase: str) -> bytearray:
    """Decrypt a v3-format envelope; return plaintext as a mutable ``bytearray``.

    Returns ``bytearray`` (not ``bytes``) so callers can zero the buffer
    after use::

        secret = decrypt_credential(keystore, pw)
        try:
            use(secret)
        finally:
            secret[:] = b"\\x00" * len(secret)

    See module docstring for SEC-006 caveats — Python may have made
    intermediate copies we can't reach.

    Raises :class:`KeystoreDecryptionError` on wrong passphrase or any
    MAC/cipher failure (fail-closed: never returns partial plaintext).
    """
    try:
        crypto = keystore_dict["crypto"]
        if crypto.get("cipher") != "aes-128-ctr":
            raise KeystoreDecryptionError(
                f"decryption failed: unsupported cipher {crypto.get('cipher')!r}"
            )
        if crypto.get("kdf") != "scrypt":
            raise KeystoreDecryptionError(
                f"decryption failed: unsupported kdf {crypto.get('kdf')!r}"
            )
        kdfparams = crypto["kdfparams"]
        salt = decode_hex(kdfparams["salt"])
        derived_key = _scrypt_hash(
            passphrase,
            salt=salt,
            buflen=kdfparams.get("dklen", DKLEN),
            r=kdfparams.get("r", SCRYPT_R),
            p=kdfparams.get("p", SCRYPT_P),
            n=kdfparams["n"],
        )
        ciphertext = decode_hex(crypto["ciphertext"])
        expected_mac = decode_hex(crypto["mac"])
        actual_mac = keccak(derived_key[16:32] + ciphertext)
        if actual_mac != expected_mac:
            raise KeystoreDecryptionError(
                "decryption failed: MAC mismatch (wrong passphrase or "
                "tampered keystore)"
            )
        iv = big_endian_to_int(decode_hex(crypto["cipherparams"]["iv"]))
        plaintext = decrypt_aes_ctr(ciphertext, derived_key[:16], iv)
    except KeystoreDecryptionError:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        # Any parse error == fail-closed. Keep the original message in the
        # exception chain for debugging but route through our typed error so
        # the CLI can map it to exit code 2.
        raise KeystoreDecryptionError(f"decryption failed: malformed keystore ({exc})") from exc

    return bytearray(plaintext)


# ---------------------------------------------------------------------------
# load_keystore — read JSON from disk; fail closed on missing file
# ---------------------------------------------------------------------------


def load_keystore(path: Path) -> dict:
    """Read and parse the keystore JSON at ``path``.

    Raises :class:`KeystoreNotFound` (with the resolved path in the message)
    if the file is absent. There is no env-var fallback — Invariant 4
    requires that missing keystore == hard stop, not "try AGENTIC_CORE_*
    env vars instead".
    """
    if not isinstance(path, Path):
        path = Path(path)
    if not path.exists():
        raise KeystoreNotFound(f"keystore not found at {path}")
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise KeystoreError(f"failed to read keystore at {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# resolve_keystore_path — R7 precedence
# ---------------------------------------------------------------------------


def resolve_keystore_path(override: str | None) -> Path:
    """Resolve the keystore path per R7 precedence.

    Order:

    1. ``override`` (the ``--admin-keystore PATH`` CLI flag value).
    2. ``$AGENTIC_CORE_ADMIN_KEYSTORE`` env var.
    3. Default ``~/.agentic-core/admin.keystore.json``.

    The function returns a :class:`Path`; it does NOT verify the path
    exists — that's :func:`load_keystore`'s job. This separation keeps
    "where should I look" distinct from "is it there" so the CLI can
    surface different error messages for the two cases.
    """
    if override:
        return Path(override).expanduser()
    env_path = os.environ.get(KEYSTORE_PATH_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser()
    return LOCAL_KEYSTORE_PATH


# ---------------------------------------------------------------------------
# read_passphrase — R1 transport contract (stdin OR fd, NEVER env var)
# ---------------------------------------------------------------------------


def read_passphrase(fd: int | None) -> str:
    """Read the keystore passphrase.

    If ``fd`` is supplied (from ``--passphrase-fd N``): read one line from
    that file descriptor and return it with a single trailing newline
    stripped. Automated callers (an agent or CI) use this path to hand the
    passphrase to the subprocess via a transient pipe so it never lands in
    ``/proc/<pid>/environ`` or shell history.

    If ``fd`` is ``None``: prompt the user interactively via
    :func:`getpass.getpass`. This is the local-dev path.

    Per R1, this function NEVER consults any env var for the passphrase.
    Env-supplied passphrases were removed in the spec review because they
    are readable via ``/proc/<pid>/environ`` for the scaffold's full
    lifetime.
    """
    if fd is not None:
        try:
            with os.fdopen(fd, "r", closefd=True) as src:
                raw = src.readline()
        except OSError as exc:
            if exc.errno == errno.EBADF:
                raise PassphraseReadError(
                    f"--passphrase-fd {fd} is not a valid open file descriptor"
                ) from exc
            raise PassphraseReadError(
                f"failed to read passphrase from fd {fd}: {exc}"
            ) from exc
        # Strip exactly one trailing newline (preserve internal whitespace).
        if raw.endswith("\n"):
            raw = raw[:-1]
        return raw

    return getpass.getpass("agentic-core admin keystore passphrase: ")


# ---------------------------------------------------------------------------
# write_keystore_atomic — for init-keystore (task #7) — chmod 0600
# ---------------------------------------------------------------------------


def write_keystore_atomic(path: Path, envelope: dict) -> None:
    """Atomically write ``envelope`` to ``path`` with mode 0600.

    Steps:

    1. ``mkdir -p`` the parent directory.
    2. Write to a temp file in the same directory (atomic-rename requires
       same filesystem).
    3. ``chmod 0600`` *before* the rename so there's no observable window
       where the file is world-readable.
    4. ``os.replace`` the temp file into place.

    Mode 0600 enforcement satisfies the SEC AC ("File mode 0600 on
    init-keystore write") and Invariant 8.
    """
    if not isinstance(path, Path):
        path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=".admin.keystore.", suffix=".json.tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(envelope, out, indent=2, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        # Belt-and-suspenders: chmod the final path too, in case the
        # filesystem's umask interactions interfered.
        os.chmod(path, 0o600)
    except Exception:
        # Clean up the temp file if anything failed before the rename.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
