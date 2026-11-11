"""Unit tests for ``agentic-core admin init-keystore`` (Track C task #7).

R20 two-phase UX flow:
  1. Read M2M id + secret from env vars (LOGTO_M2M_APP_ID/_SECRET) OR prompt
  2. Read passphrase via getpass (with confirm)
  3. Print summary (id only — NEVER secret); ask for typer.confirm
  4. Write keystore (atomic, 0600)
  5. Verify: re-decrypt and assert equality
  6. Verify: mint a Logto management token with the decrypted credential
  7. Print instruction to unset env vars

Critical security AC:
  - File mode 0600 on write
  - The plaintext secret NEVER appears in stdout or stderr capture
  - Fails closed if user declines confirm (no file written)
  - --json mode emits exactly one JSON line to stdout

Tests use Typer's CliRunner so stdin/getpass are easy to monkeypatch and we
get full capture of stdout/stderr without spawning a subprocess.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import responses
from typer.testing import CliRunner
from tests.conftest import plain

from agentic_core import admin, cli
from agentic_core.bootstrap import keystore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SECRET_VALUE = "super-secret-admin-m2m-998877"
APP_ID_VALUE = "admin-m2m-id-1234"
PASSPHRASE = "correct-horse-battery-staple"
LOGTO_ENDPOINT = "https://logto.test.example.com"


@pytest.fixture
def runner() -> CliRunner:
    # Click 8.4+ captures stdout and stderr separately by default — we
    # need them split so we can assert R2 stdout discipline (JSON only on
    # stdout in --json mode; Rich/human output on stderr).
    return CliRunner()


@pytest.fixture
def tmp_keystore_path(tmp_path) -> Path:
    return tmp_path / "admin.keystore.json"


@pytest.fixture
def _no_real_paths(monkeypatch, tmp_path):
    """Re-point the keystore path default so we never write to ~."""
    monkeypatch.setattr(
        keystore, "LOCAL_KEYSTORE_PATH", tmp_path / "local" / "admin.keystore.json"
    )
    yield


#: The real loader, kept before the autouse fixture replaces it.
_REAL_LOAD_ENV_FILES = cli._load_env_files


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip Logto env vars from the test environment so prompts kick in
    deterministically unless a test explicitly sets them."""
    monkeypatch.delenv("LOGTO_M2M_APP_ID", raising=False)
    monkeypatch.delenv("LOGTO_M2M_APP_SECRET", raising=False)
    monkeypatch.delenv("LOGTO_ENDPOINT", raising=False)
    monkeypatch.delenv("AGENTIC_CORE_ADMIN_KEYSTORE", raising=False)
    # Never load the developer's real ./.env or ~/.agentic-apps.env.
    monkeypatch.setattr(cli, "_load_env_files", lambda: None)
    yield


def _stub_logto_token_endpoint(success: bool = True) -> None:
    """Stub the OIDC token endpoint used by the post-write Logto verify step."""
    if success:
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/oidc/token",
            json={"access_token": "verify-ok", "expires_in": 3600, "token_type": "Bearer"},
            status=200,
        )
    else:
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/oidc/token",
            json={"error": "invalid_client"},
            status=401,
        )


# ---------------------------------------------------------------------------
# admin module surface
# ---------------------------------------------------------------------------


class TestAdminAppRegistration:
    """The ``admin`` sub-app is mounted on the root CLI; --help works."""

    def test_admin_sub_app_exists(self):
        assert hasattr(admin, "admin_app")
        # Mounted on the root Typer.
        # Typer stores added sub-apps on `registered_groups`.
        names = [g.name for g in cli.app.registered_groups]
        assert "admin" in names

    def test_admin_help_lists_init_keystore(self, runner):
        result = runner.invoke(cli.app, ["admin", "--help"])
        assert result.exit_code == 0
        assert "init-keystore" in result.stdout

    def test_init_keystore_help_works(self, runner):
        result = runner.invoke(cli.app, ["admin", "init-keystore", "--help"])
        assert result.exit_code == 0
        help_text = plain(result.stdout)
        # Documents the flags we promise.
        assert "--keystore-path" in help_text
        assert "--passphrase-fd" in help_text
        # Critically, --m2m-secret is NOT a flag (would leak via argv).
        assert "--m2m-secret" not in help_text


# ---------------------------------------------------------------------------
# Happy path: env vars + passphrase via fd + auto-confirm
# ---------------------------------------------------------------------------


class TestInitKeystoreHappyPath:

    @responses.activate
    def test_writes_file_mode_0600(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", APP_ID_VALUE)
        monkeypatch.setenv("LOGTO_M2M_APP_SECRET", SECRET_VALUE)
        monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)
        _stub_logto_token_endpoint(success=True)

        # Pass passphrase via fd to avoid getpass.
        r_fd, w_fd = os.pipe()
        os.write(w_fd, (PASSPHRASE + "\n").encode())
        os.close(w_fd)

        result = runner.invoke(
            cli.app,
            [
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--passphrase-fd",
                str(r_fd),
                "--yes",
            ],
        )

        assert result.exit_code == 0, (
            f"stdout={result.stdout!r}\nstderr={result.stderr!r}\nexc={result.exception}"
        )
        assert tmp_keystore_path.exists()
        mode = stat.S_IMODE(os.stat(tmp_keystore_path).st_mode)
        assert mode == 0o600, f"file mode is 0o{mode:o}, expected 0o600"

    @responses.activate
    def test_decryption_round_trip_succeeds(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", APP_ID_VALUE)
        monkeypatch.setenv("LOGTO_M2M_APP_SECRET", SECRET_VALUE)
        monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)
        _stub_logto_token_endpoint(success=True)

        r_fd, w_fd = os.pipe()
        os.write(w_fd, (PASSPHRASE + "\n").encode())
        os.close(w_fd)

        result = runner.invoke(
            cli.app,
            [
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--passphrase-fd",
                str(r_fd),
                "--yes",
            ],
        )
        assert result.exit_code == 0
        envelope = json.loads(tmp_keystore_path.read_text())
        # Decrypt with the same passphrase — must equal the original.
        secret = keystore.decrypt_credential(envelope, PASSPHRASE)
        assert bytes(secret) == SECRET_VALUE.encode("utf-8")

    @responses.activate
    def test_no_plaintext_secret_in_stdout_or_stderr(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        """SEC AC: the M2M secret must NEVER appear in any captured output."""
        monkeypatch.setenv("LOGTO_M2M_APP_ID", APP_ID_VALUE)
        monkeypatch.setenv("LOGTO_M2M_APP_SECRET", SECRET_VALUE)
        monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)
        _stub_logto_token_endpoint(success=True)

        r_fd, w_fd = os.pipe()
        os.write(w_fd, (PASSPHRASE + "\n").encode())
        os.close(w_fd)

        result = runner.invoke(
            cli.app,
            [
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--passphrase-fd",
                str(r_fd),
                "--yes",
            ],
        )
        assert result.exit_code == 0
        assert SECRET_VALUE not in result.stdout, (
            "M2M secret leaked into stdout"
        )
        assert SECRET_VALUE not in result.stderr, (
            "M2M secret leaked into stderr"
        )
        # Also: passphrase should not leak.
        assert PASSPHRASE not in result.stdout
        assert PASSPHRASE not in result.stderr

    @responses.activate
    def test_app_id_shown_in_summary(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        """R20 step 3: summary shows app_id (non-secret) for operator review."""
        monkeypatch.setenv("LOGTO_M2M_APP_ID", APP_ID_VALUE)
        monkeypatch.setenv("LOGTO_M2M_APP_SECRET", SECRET_VALUE)
        monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)
        _stub_logto_token_endpoint(success=True)

        r_fd, w_fd = os.pipe()
        os.write(w_fd, (PASSPHRASE + "\n").encode())
        os.close(w_fd)

        result = runner.invoke(
            cli.app,
            [
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--passphrase-fd",
                str(r_fd),
                "--yes",
            ],
        )
        assert result.exit_code == 0
        # Summary appears on stderr (R2 stdout discipline).
        assert APP_ID_VALUE in result.stderr
        # Path appears, but Rich may wrap long paths across lines for
        # terminal width — assert on the basename, which is robust.
        assert tmp_keystore_path.name in result.stderr

    @responses.activate
    def test_prints_unset_env_var_instruction(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        """R20 step 7: print instruction to clean up env vars."""
        monkeypatch.setenv("LOGTO_M2M_APP_ID", APP_ID_VALUE)
        monkeypatch.setenv("LOGTO_M2M_APP_SECRET", SECRET_VALUE)
        monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)
        _stub_logto_token_endpoint(success=True)

        r_fd, w_fd = os.pipe()
        os.write(w_fd, (PASSPHRASE + "\n").encode())
        os.close(w_fd)

        result = runner.invoke(
            cli.app,
            [
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--passphrase-fd",
                str(r_fd),
                "--yes",
            ],
        )
        assert result.exit_code == 0
        # Remove the secret only; preflight still needs the (non-secret) ID.
        assert "unset LOGTO_M2M_APP_SECRET" in result.stderr
        assert "unset LOGTO_M2M_APP_ID" not in result.stderr
        assert "Keep LOGTO_M2M_APP_ID" in result.stderr


# ---------------------------------------------------------------------------
# --json mode
# ---------------------------------------------------------------------------


class TestInitKeystoreJsonMode:

    @responses.activate
    def test_json_mode_emits_single_json_line_to_stdout(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        monkeypatch.setenv("LOGTO_M2M_APP_ID", APP_ID_VALUE)
        monkeypatch.setenv("LOGTO_M2M_APP_SECRET", SECRET_VALUE)
        monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)
        _stub_logto_token_endpoint(success=True)

        r_fd, w_fd = os.pipe()
        os.write(w_fd, (PASSPHRASE + "\n").encode())
        os.close(w_fd)

        result = runner.invoke(
            cli.app,
            [
                "--json",
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--passphrase-fd",
                str(r_fd),
                "--yes",
            ],
        )
        assert result.exit_code == 0
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, f"stdout should be 1 JSON line, got {lines!r}"
        payload = json.loads(lines[0])
        assert payload["status"] == "success"
        assert payload["keystore_path"] == str(tmp_keystore_path)
        # Secret MUST NOT appear in the success envelope.
        assert SECRET_VALUE not in lines[0]


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


class TestInitKeystoreFailures:

    @responses.activate
    def test_confirm_decline_does_not_write_file(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        """Operator declining the confirm prompt aborts cleanly with no file."""
        monkeypatch.setenv("LOGTO_M2M_APP_ID", APP_ID_VALUE)
        monkeypatch.setenv("LOGTO_M2M_APP_SECRET", SECRET_VALUE)
        monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)

        r_fd, w_fd = os.pipe()
        os.write(w_fd, (PASSPHRASE + "\n").encode())
        os.close(w_fd)

        # Stdin "n\n" answers no to the confirm prompt. The assertion below
        # is on the filesystem, not the exit code, so the result is unused.
        runner.invoke(
            cli.app,
            [
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--passphrase-fd",
                str(r_fd),
            ],
            input="n\n",
        )
        # Aborted — non-zero exit OK; the critical assertion is no file.
        assert not tmp_keystore_path.exists(), "decline must not write file"

    @responses.activate
    def test_logto_verify_failure_surfaces_error_no_secret_leak(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        """If post-write Logto verify call fails (401), exit non-zero — but
        the secret still must not appear in any output."""
        monkeypatch.setenv("LOGTO_M2M_APP_ID", APP_ID_VALUE)
        monkeypatch.setenv("LOGTO_M2M_APP_SECRET", SECRET_VALUE)
        monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)
        _stub_logto_token_endpoint(success=False)

        r_fd, w_fd = os.pipe()
        os.write(w_fd, (PASSPHRASE + "\n").encode())
        os.close(w_fd)

        result = runner.invoke(
            cli.app,
            [
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--passphrase-fd",
                str(r_fd),
                "--yes",
            ],
        )
        assert result.exit_code != 0
        # Critical: even on failure, never leak the secret.
        assert SECRET_VALUE not in result.stdout
        assert SECRET_VALUE not in result.stderr

    def test_missing_id_and_secret_prompts_or_errors(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        """Without env vars or flags, --yes mode must error (can't auto-confirm
        without an id at minimum). The CliRunner has no TTY so getpass would
        raise; we expect a clean non-zero exit."""
        # No env vars, no --m2m-id flag, --yes with no stdin → fail.
        result = runner.invoke(
            cli.app,
            [
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--yes",
            ],
        )
        assert result.exit_code != 0
        assert not tmp_keystore_path.exists()


class TestInitKeystoreNeedsEndpoint:
    """There is no built-in Logto endpoint. Without --logto-endpoint or
    LOGTO_ENDPOINT, init-keystore fails before it reads any secret."""

    def test_missing_endpoint_fails_before_reading_secrets(
        self, runner, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        read_calls: list[str] = []
        monkeypatch.setattr(
            admin,
            "_read_m2m_credential",
            lambda m2m_id: read_calls.append("m2m") or ("id", bytearray(b"s")),
        )
        result = runner.invoke(
            cli.app,
            [
                "--json",
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--yes",
            ],
        )
        assert result.exit_code == 5
        assert read_calls == []
        assert "LOGTO_ENDPOINT" in result.stdout
        assert not tmp_keystore_path.exists()


class TestInitKeystoreReadsDotEnv:
    """The README puts LOGTO_ENDPOINT in .env. init-keystore must read it."""

    def test_endpoint_in_dot_env_passes_the_endpoint_check(
        self, runner, tmp_path, tmp_keystore_path, monkeypatch, _no_real_paths
    ):
        monkeypatch.setattr(cli, "_load_env_files", _REAL_LOAD_ENV_FILES)
        work = tmp_path / "work"
        work.mkdir()
        (work / ".env").write_text("LOGTO_ENDPOINT=https://from-dotenv.example\n")
        monkeypatch.chdir(work)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        # Register LOGTO_ENDPOINT with monkeypatch so the value dotenv sets is
        # removed after the test.
        monkeypatch.setenv("LOGTO_ENDPOINT", "placeholder")
        monkeypatch.delenv("LOGTO_ENDPOINT")
        seen: list[str | None] = []

        def _read(m2m_id):
            seen.append(os.environ.get("LOGTO_ENDPOINT"))
            raise admin.typer.BadParameter("stop after the endpoint check")

        monkeypatch.setattr(admin, "_read_m2m_credential", _read)
        runner.invoke(
            cli.app,
            [
                "--json",
                "admin",
                "init-keystore",
                "--keystore-path",
                str(tmp_keystore_path),
                "--yes",
            ],
        )
        assert seen == ["https://from-dotenv.example"]
