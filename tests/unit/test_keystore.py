"""Unit tests for agentic_core.bootstrap.keystore.

Covers Track C invariants:
- R1 passphrase transport (stdin OR --passphrase-fd, NEVER env var)
- R7 keystore path precedence (--admin-keystore > env > ~/.agentic-core/...)
- SEC-006 mutable bytearray return so callers can zero secrets after use
- Invariant 4 fail-closed posture: no env-var fallback on missing file or bad
  passphrase

These tests stub stdin/file descriptors and use ``tmp_path`` for keystore I/O —
nothing real (no Logto, no Railway) is touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from agentic_core.bootstrap import keystore


# ---------------------------------------------------------------------------
# encrypt_credential / decrypt_credential round-trip
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip_short_secret():
    """A short (Logto-secret-sized) payload survives an encrypt/decrypt cycle."""
    secret = b"some-logto-m2m-secret-1234567890"
    envelope = keystore.encrypt_credential(secret, "correct-horse-battery-staple")
    out = keystore.decrypt_credential(envelope, "correct-horse-battery-staple")
    assert bytes(out) == secret
    assert isinstance(out, bytearray), "must be mutable so callers can zero it"


def test_encrypt_decrypt_round_trip_json_credential():
    """A JSON-serialized credential bundle (the real shape) round-trips."""
    credential = json.dumps(
        {
            "app_id": "abcdef1234567890",
            "app_secret": "supersecret-management-api-token-value",
        }
    ).encode("utf-8")
    envelope = keystore.encrypt_credential(credential, "hunter2")
    out = keystore.decrypt_credential(envelope, "hunter2")
    assert bytes(out) == credential


def test_envelope_is_v3_compatible_shape():
    """Envelope must use the eth-account v3 JSON layout (scrypt + AES-128-CTR)."""
    envelope = keystore.encrypt_credential(b"x" * 16, "pw")
    assert envelope["version"] == 3
    assert envelope["crypto"]["cipher"] == "aes-128-ctr"
    assert envelope["crypto"]["kdf"] == "scrypt"
    # The crypto sub-block must carry the expected keys.
    crypto = envelope["crypto"]
    assert "ciphertext" in crypto
    assert "cipherparams" in crypto and "iv" in crypto["cipherparams"]
    assert "kdfparams" in crypto
    assert "mac" in crypto
    # Each envelope gets a fresh UUID.
    assert "id" in envelope


def test_encrypt_produces_distinct_envelopes_for_same_input():
    """Salt + IV randomization means two encrypts of the same payload differ."""
    secret = b"identical-payload"
    a = keystore.encrypt_credential(secret, "pw")
    b = keystore.encrypt_credential(secret, "pw")
    assert a["crypto"]["ciphertext"] != b["crypto"]["ciphertext"]
    assert a["crypto"]["cipherparams"]["iv"] != b["crypto"]["cipherparams"]["iv"]
    assert a["crypto"]["kdfparams"]["salt"] != b["crypto"]["kdfparams"]["salt"]


def test_decrypt_fails_closed_on_bad_passphrase():
    """Wrong passphrase raises KeystoreDecryptionError with the expected message."""
    envelope = keystore.encrypt_credential(b"secret-data-here", "right-pw")
    with pytest.raises(keystore.KeystoreDecryptionError) as exc:
        keystore.decrypt_credential(envelope, "wrong-pw")
    assert "decryption failed" in str(exc.value).lower()


def test_decrypt_fails_closed_on_tampered_ciphertext():
    """Tampered ciphertext (MAC mismatch) raises KeystoreDecryptionError."""
    envelope = keystore.encrypt_credential(b"secret-data-here", "pw")
    # Flip a byte in the ciphertext.
    ct = bytearray.fromhex(envelope["crypto"]["ciphertext"])
    ct[0] ^= 0xFF
    envelope["crypto"]["ciphertext"] = ct.hex()
    with pytest.raises(keystore.KeystoreDecryptionError):
        keystore.decrypt_credential(envelope, "pw")


# ---------------------------------------------------------------------------
# load_keystore — missing file fails closed (no env-var fallback)
# ---------------------------------------------------------------------------


def test_load_keystore_raises_keystore_not_found_when_missing(tmp_path: Path):
    """Missing keystore raises KeystoreNotFound with 'keystore not found' in msg."""
    missing = tmp_path / "does-not-exist.keystore.json"
    with pytest.raises(keystore.KeystoreNotFound) as exc:
        keystore.load_keystore(missing)
    assert "keystore not found" in str(exc.value).lower()
    assert str(missing) in str(exc.value)


def test_load_keystore_returns_parsed_json(tmp_path: Path):
    """A valid keystore file is loaded and parsed."""
    envelope = keystore.encrypt_credential(b"payload", "pw")
    path = tmp_path / "admin.keystore.json"
    path.write_text(json.dumps(envelope))
    loaded = keystore.load_keystore(path)
    assert loaded == envelope


def test_load_keystore_does_not_fall_back_to_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Even if AGENTIC_CORE_ADMIN_PASSPHRASE is set, missing file fails closed.

    Guards Invariant 4 — no env-var fallback for the admin credential.
    """
    monkeypatch.setenv("AGENTIC_CORE_ADMIN_PASSPHRASE", "doesntmatter")
    monkeypatch.setenv("LOGTO_M2M_APP_ID", "old-shared-id")
    monkeypatch.setenv("LOGTO_M2M_APP_SECRET", "old-shared-secret")
    with pytest.raises(keystore.KeystoreNotFound):
        keystore.load_keystore(tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# resolve_keystore_path — R7 precedence rules
# ---------------------------------------------------------------------------


def test_resolve_path_prefers_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """--admin-keystore flag wins over env var and defaults."""
    monkeypatch.setenv("AGENTIC_CORE_ADMIN_KEYSTORE", str(tmp_path / "env.json"))
    override = tmp_path / "explicit.json"
    resolved = keystore.resolve_keystore_path(str(override))
    assert resolved == override


def test_resolve_path_uses_env_var_when_no_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """AGENTIC_CORE_ADMIN_KEYSTORE wins when no --admin-keystore flag."""
    env_path = tmp_path / "from-env.json"
    monkeypatch.setenv("AGENTIC_CORE_ADMIN_KEYSTORE", str(env_path))
    resolved = keystore.resolve_keystore_path(None)
    assert resolved == env_path


def test_resolve_path_falls_back_to_local_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The home-folder default wins when no override and no env var."""
    monkeypatch.delenv("AGENTIC_CORE_ADMIN_KEYSTORE", raising=False)
    local = tmp_path / "home" / ".agentic-core" / "admin.keystore.json"
    monkeypatch.setattr(keystore, "LOCAL_KEYSTORE_PATH", local)
    resolved = keystore.resolve_keystore_path(None)
    assert resolved == local


def test_resolve_path_has_no_machine_specific_default():
    """Only the flag, the env var, and the home default exist. A fixed server
    path would point other people at a folder they do not have."""
    assert not hasattr(keystore, "VPS_KEYSTORE_DIR")
    assert not hasattr(keystore, "VPS_KEYSTORE_PATH")


# ---------------------------------------------------------------------------
# read_passphrase — R1 transport contract
# ---------------------------------------------------------------------------


def test_read_passphrase_from_fd_reads_until_newline(tmp_path: Path):
    """--passphrase-fd reads one line from the given file descriptor."""
    r, w = os.pipe()
    try:
        os.write(w, b"my-passphrase\n")
        os.close(w)
        pw = keystore.read_passphrase(fd=r)
        assert pw == "my-passphrase"
    finally:
        # ``r`` is closed by read_passphrase; nothing else to clean up.
        pass


def test_read_passphrase_from_fd_strips_only_trailing_newline():
    """Internal whitespace in a passphrase is preserved."""
    r, w = os.pipe()
    os.write(w, b"my pass with spaces\n")
    os.close(w)
    pw = keystore.read_passphrase(fd=r)
    assert pw == "my pass with spaces"


def test_read_passphrase_from_fd_handles_no_trailing_newline():
    """A passphrase written without a newline still works."""
    r, w = os.pipe()
    os.write(w, b"no-newline-here")
    os.close(w)
    pw = keystore.read_passphrase(fd=r)
    assert pw == "no-newline-here"


def test_read_passphrase_falls_back_to_getpass_when_no_fd():
    """No fd → interactive getpass prompt (stdin path for local dev)."""
    with mock.patch("agentic_core.bootstrap.keystore.getpass.getpass") as gp:
        gp.return_value = "interactive-pw"
        pw = keystore.read_passphrase(fd=None)
    assert pw == "interactive-pw"
    gp.assert_called_once()


def test_read_passphrase_never_reads_env_var(monkeypatch: pytest.MonkeyPatch):
    """The env-var path is gone — even if set, getpass is called.

    Guards R1: env vars are readable via /proc/<pid>/environ for the lifetime
    of the process; we must not depend on them for passphrase transport.
    """
    monkeypatch.setenv("AGENTIC_CORE_ADMIN_PASSPHRASE", "should-be-ignored")
    with mock.patch("agentic_core.bootstrap.keystore.getpass.getpass") as gp:
        gp.return_value = "from-stdin"
        pw = keystore.read_passphrase(fd=None)
    assert pw == "from-stdin"
    assert pw != "should-be-ignored"


def test_read_passphrase_fd_failure_raises_clearly():
    """A closed fd raises a meaningful PassphraseReadError, not a bare OSError."""
    # Pick an fd that's almost certainly invalid.
    bad_fd = 999_999
    with pytest.raises(keystore.PassphraseReadError):
        keystore.read_passphrase(fd=bad_fd)


# ---------------------------------------------------------------------------
# write_keystore_atomic — file mode 0600 on init-keystore write
# ---------------------------------------------------------------------------


def test_write_keystore_atomic_sets_mode_0600(tmp_path: Path):
    """init-keystore writes must chmod 0600 after the write (SEC, Invariant 8)."""
    envelope = keystore.encrypt_credential(b"secret-blob", "pw")
    out = tmp_path / "admin.keystore.json"
    keystore.write_keystore_atomic(out, envelope)
    assert out.exists()
    # Mode mask 0o777 covers the standard rwx bits we care about.
    mode = out.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    # Sanity: contents round-trip via load_keystore.
    assert keystore.load_keystore(out) == envelope


def test_write_keystore_atomic_creates_parent_dirs(tmp_path: Path):
    """Writing to a nested path auto-creates the parent directory tree."""
    envelope = keystore.encrypt_credential(b"x", "pw")
    out = tmp_path / "nested" / "subdir" / "admin.keystore.json"
    keystore.write_keystore_atomic(out, envelope)
    assert out.exists()
    assert (out.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# Spec invariant: module never references AGENTIC_CORE_ADMIN_PASSPHRASE
# ---------------------------------------------------------------------------


def test_keystore_module_does_not_reference_passphrase_env_var():
    """No code path may read AGENTIC_CORE_ADMIN_PASSPHRASE (R1 invariant).

    This is the test-side mirror of the grep AC; it pins the invariant in
    pytest so a future refactor can't silently re-introduce the env-var path.
    """
    source = Path(keystore.__file__).read_text()
    assert "AGENTIC_CORE_ADMIN_PASSPHRASE" not in source, (
        "keystore.py must not reference AGENTIC_CORE_ADMIN_PASSPHRASE; "
        "passphrase transport is stdin-or-fd only (R1)."
    )
