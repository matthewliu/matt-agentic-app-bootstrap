"""Tests for ``agentic_core.bootstrap.state``.

Covers spec §R8 (state file security): mode 0600 on every write, no secrets
persisted, fail-closed on corrupt JSON, delete-on-success contract. Also
covers §R18 (the ``scaffolded_by`` field added to the schema).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, UTC
from pathlib import Path

import pytest

from agentic_core.bootstrap.state import (
    BootstrapState,
    StateFileCorrupt,
    StateFileEntropyError,
    delete_state,
    read_state,
    write_state,
)


# Realistic resource-ID shapes drawn from what each platform actually returns:
#   - Logto app IDs are 21-char ulid-like — under the 32-char gate, safe.
#   - GitHub repo URLs are short ("example-owner/foo") — safe.
#   - Railway project IDs are 36-char UUID v4 WITH hyphens. Hyphens segment
#     the UUID into 8/4/4/4/12-char runs, so the gate (which excludes ``-``
#     from its character class) sees no 32+ run. We use a full-length
#     hyphenated UUID here so the test exercises the real shape the
#     orchestrator (Task #8) will write back from a Railway API call —
#     not a sanitized short stand-in.
REALISTIC_IDS = {
    "logto_m2m_app_id": "abc123xyz789ulid01234",  # 21 chars, ulid-shape
    "logto_spa_client_id": "spaclient0123456789ab",  # 21 chars, ulid-shape
    "logto_api_resource": "https://api.foo.dev",
    "logto_user_sub": "user_abc1234567",
    "github_repo_url": "https://github.com/example-owner/foo",
    # Realistic Railway UUID v4 — 36 chars total but hyphens split it into
    # max-12-char runs. With hyphen excluded from the entropy class, this
    # does NOT match. This is the load-bearing test fixture for the regex
    # tightening called out in the wave-2 review.
    "railway_project_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "railway_service_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
}

# Regex from the spec / acceptance gate (R8 entropy gate). Hyphen is
# deliberately excluded — see module docstring in state.py and the
# REALISTIC_IDS comment above for the rationale.
#
# NOTE: the RUNTIME gate (state.write_state) applies this pattern only to
# NON-URL ``resource_ids`` values (values containing ``"://"`` are exempt,
# since ``/`` is in the class but does not segment a URL). The whole-payload
# ``findall`` checks below are therefore a STRICTER, test-only assertion that
# holds because REALISTIC_IDS uses short, hyphen-segmented values — NOT a
# byte-for-byte mirror of the runtime scan. See
# TestEntropyGate::test_long_hyphenless_repo_url_does_not_trip_gate for the
# URL case the runtime gate exempts but a whole-payload scan would flag.
ENTROPY_RE = re.compile(r"[A-Za-z0-9+/=_]{32,}")


def _sample_state(**overrides) -> BootstrapState:
    base = dict(
        app_slug="foo",
        variant="full",
        scaffolded_by="manual",
        created_at=datetime(2026, 5, 23, 12, 0, tzinfo=UTC),
        checkpoints={"logto": True, "railway": False},
        resource_ids=dict(REALISTIC_IDS),
        last_completed_step="logto_provisioning",
    )
    base.update(overrides)
    return BootstrapState(**base)


class TestSchema:
    def test_schema_version_defaults_to_1(self):
        s = _sample_state()
        assert s.schema_version == 1

    def test_scaffolded_by_accepts_agent(self):
        s = _sample_state(scaffolded_by="agent")
        assert s.scaffolded_by == "agent"

    def test_scaffolded_by_accepts_manual(self):
        s = _sample_state(scaffolded_by="manual")
        assert s.scaffolded_by == "manual"

    def test_scaffolded_by_rejects_unknown(self):
        # Literal-validated; pydantic v2 raises ValidationError.
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            _sample_state(scaffolded_by="robot")

    def test_last_completed_step_can_be_none(self):
        s = _sample_state(last_completed_step=None)
        assert s.last_completed_step is None


class TestDeployTarget:
    """KTD8: the state file records the deploy target."""

    def test_defaults_to_railway(self):
        assert _sample_state().deploy_target == "railway"

    def test_rejects_unknown_target(self):
        with pytest.raises(ValueError):
            _sample_state(deploy_target="heroku")

    def test_old_state_file_without_field_loads_as_railway(self, tmp_path: Path):
        p = tmp_path / ".bootstrap-state.json"
        payload = json.loads(_sample_state().model_dump_json())
        del payload["deploy_target"]
        p.write_text(json.dumps(payload))
        assert read_state(p).deploy_target == "railway"

    def test_render_round_trip(self, tmp_path: Path):
        p = tmp_path / ".bootstrap-state.json"
        write_state(p, _sample_state(deploy_target="render"))
        assert read_state(p).deploy_target == "render"


class TestWriteState:
    def test_creates_file(self, tmp_path: Path):
        p = tmp_path / ".bootstrap-state.json"
        write_state(p, _sample_state())
        assert p.exists()

    def test_file_mode_is_0600(self, tmp_path: Path):
        # AC: state file mode 0600 after every write.
        p = tmp_path / ".bootstrap-state.json"
        write_state(p, _sample_state())
        mode = oct(p.stat().st_mode & 0o777)[-3:]
        assert mode == "600", f"expected mode 600, got {mode}"

    def test_file_mode_is_0600_on_overwrite(self, tmp_path: Path):
        # Write twice; the second write must still chmod 0600.
        p = tmp_path / ".bootstrap-state.json"
        write_state(p, _sample_state())
        # Loosen mode to simulate a tampered file.
        p.chmod(0o644)
        write_state(p, _sample_state(last_completed_step="railway_provisioning"))
        mode = oct(p.stat().st_mode & 0o777)[-3:]
        assert mode == "600", f"expected mode 600 after overwrite, got {mode}"

    def test_file_is_valid_json(self, tmp_path: Path):
        p = tmp_path / ".bootstrap-state.json"
        write_state(p, _sample_state())
        data = json.loads(p.read_text())
        assert data["schema_version"] == 1
        assert data["app_slug"] == "foo"
        assert data["scaffolded_by"] == "manual"

    def test_creates_parent_directory(self, tmp_path: Path):
        nested = tmp_path / "sub" / "dir" / ".bootstrap-state.json"
        write_state(nested, _sample_state())
        assert nested.exists()


class TestEntropyGate:
    def test_no_high_entropy_strings_in_dumped_json(self, tmp_path: Path):
        # AC: state file contains zero strings matching /[A-Za-z0-9+/=_]{32,}/.
        # This is the R8 entropy gate — proves we are not persisting secrets.
        # The fixture includes a full 36-char Railway UUID v4 with hyphens to
        # prove that real-world resource IDs do not trip the gate.
        p = tmp_path / ".bootstrap-state.json"
        write_state(p, _sample_state())
        raw = p.read_text()
        matches = ENTROPY_RE.findall(raw)
        assert matches == [], f"high-entropy strings leaked into state: {matches}"

    def test_model_dump_json_has_no_high_entropy_strings(self):
        # Direct check on the in-memory serialization path.
        s = _sample_state()
        payload = s.model_dump_json()
        matches = ENTROPY_RE.findall(payload)
        assert matches == [], f"high-entropy strings in model_dump_json: {matches}"

    def test_railway_project_id_uuid_write_does_not_raise_or_trip_gate(
        self, tmp_path: Path
    ):
        # R2 (U3): the orchestrator persists the parsed `railway init --json`
        # project id under `railway_project_id`. A 36-char hyphenated UUID must
        # (a) write without error and (b) not trip the R8 entropy gate.
        p = tmp_path / ".bootstrap-state.json"
        s = _sample_state(
            resource_ids={
                "railway_project_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
            }
        )
        write_state(p, s)  # must not raise
        raw = p.read_text()
        assert "6ba7b810-9dad-11d1-80b4-00c04fd430c8" in raw
        assert ENTROPY_RE.findall(raw) == []

    def test_write_state_raises_on_secret_shaped_value(self, tmp_path: Path):
        # Fix 3: the R8 entropy gate must be a RUNTIME guard, not test-only.
        # A hyphen-free 40-char high-entropy run (secret-shaped) must make
        # write_state fail fast BEFORE anything is written to disk.
        p = tmp_path / ".bootstrap-state.json"
        secret = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"  # 40 chars, no hyphens
        assert ENTROPY_RE.findall(secret) == [secret]  # confirms it matches the gate
        s = _sample_state(resource_ids={"leaked_secret": secret})
        with pytest.raises(StateFileEntropyError):
            write_state(p, s)
        # Fail-closed: nothing was written.
        assert not p.exists()

    def test_long_hyphenless_repo_url_does_not_trip_gate(self, tmp_path: Path):
        # Finding 3 (regression): a real GitHub repo URL with a long hyphen-free
        # path — e.g. github.com/exampleowner/longexampleappname — contains a
        # contiguous [A-Za-z0-9+/=_] run over 32 chars (slashes DON'T segment,
        # they're in the class). A naive whole-payload scan false-positives and
        # kills ALL state checkpointing for that app. The URL-aware gate must
        # exempt ``://``-shaped values, so this write MUST succeed.
        p = tmp_path / ".bootstrap-state.json"
        s = _sample_state(
            resource_ids={
                "github_repo_url": "https://github.com/exampleowner/longexampleappname"
            }
        )
        write_state(p, s)  # must NOT raise
        assert p.exists()
        assert (
            "https://github.com/exampleowner/longexampleappname" in p.read_text()
        )

    def test_credential_bearing_url_is_not_exempt(self, tmp_path: Path):
        # Fix 14: the ``://`` exemption is for PLAIN URLs (repo URLs, indicator
        # URLs, hostnames) only. A credential-bearing URL — one with userinfo
        # (``scheme://user:pass@host``), the likeliest secret sink (a
        # DATABASE_URL) — must NOT be exempt and must be refused.
        p = tmp_path / ".bootstrap-state.json"
        s = _sample_state(
            resource_ids={
                "database_url": "postgres://user:SUPERSECRETPASSWORD123456789@host:5432/db"
            }
        )
        with pytest.raises(StateFileEntropyError):
            write_state(p, s)
        # Fail-closed: nothing was written.
        assert not p.exists()

    def test_plain_url_without_userinfo_still_exempt(self, tmp_path: Path):
        # Fix 14 (regression guard): a plain URL with NO userinfo stays exempt
        # even with a long hyphen-free path.
        p = tmp_path / ".bootstrap-state.json"
        s = _sample_state(
            resource_ids={
                "github_repo_url": "https://github.com/exampleowner/longexampleappname"
            }
        )
        write_state(p, s)  # must NOT raise
        assert p.exists()

    def test_write_state_error_message_does_not_leak_the_secret(self, tmp_path: Path):
        # The guard must not echo the offending value into logs/exceptions.
        p = tmp_path / ".bootstrap-state.json"
        secret = "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2H1g0"  # 40 chars, no hyphens
        s = _sample_state(resource_ids={"leaked_secret": secret})
        with pytest.raises(StateFileEntropyError) as exc:
            write_state(p, s)
        assert secret not in str(exc.value)


class TestReadState:
    def test_round_trip(self, tmp_path: Path):
        p = tmp_path / ".bootstrap-state.json"
        original = _sample_state()
        write_state(p, original)
        loaded = read_state(p)
        assert loaded.app_slug == original.app_slug
        assert loaded.variant == original.variant
        assert loaded.scaffolded_by == original.scaffolded_by
        assert loaded.checkpoints == original.checkpoints
        assert loaded.resource_ids == original.resource_ids
        assert loaded.last_completed_step == original.last_completed_step
        assert loaded.schema_version == 1

    def test_corrupt_garbage_raises(self, tmp_path: Path):
        # AC: corrupt state file raises StateFileCorrupt.
        p = tmp_path / ".bootstrap-state.json"
        p.write_text("not-valid-json{")
        with pytest.raises(StateFileCorrupt):
            read_state(p)

    def test_corrupt_truncated_raises(self, tmp_path: Path):
        p = tmp_path / ".bootstrap-state.json"
        # Write valid state, then truncate to simulate crash mid-write.
        write_state(p, _sample_state())
        full = p.read_text()
        p.write_text(full[: len(full) // 2])
        with pytest.raises(StateFileCorrupt):
            read_state(p)

    def test_missing_required_field_raises(self, tmp_path: Path):
        p = tmp_path / ".bootstrap-state.json"
        # Missing app_slug — should fail validation, surfaced as corrupt.
        p.write_text(json.dumps({"schema_version": 1, "variant": "full"}))
        with pytest.raises(StateFileCorrupt):
            read_state(p)

    def test_missing_file_raises_filenotfound(self, tmp_path: Path):
        # We don't bury FileNotFoundError in StateFileCorrupt — the caller
        # should be able to distinguish "no state yet" from "corrupt state".
        p = tmp_path / "does-not-exist.json"
        with pytest.raises(FileNotFoundError):
            read_state(p)


class TestDeleteState:
    def test_deletes_existing_file(self, tmp_path: Path):
        p = tmp_path / ".bootstrap-state.json"
        write_state(p, _sample_state())
        assert p.exists()
        delete_state(p)
        assert not p.exists()

    def test_missing_file_is_noop(self, tmp_path: Path):
        # On successful scaffold, orchestrator calls delete_state. If a
        # previous run was already cleaned up (or never started), this must
        # not crash.
        p = tmp_path / "never-existed.json"
        delete_state(p)  # should not raise
        assert not p.exists()


class TestNoSecretsLeakage:
    def test_resource_ids_only_stores_ids(self):
        # Sanity: the model accepts only resource IDs, not secrets.
        # The entropy gate above is the mechanical guard; this is a
        # documentation test for future maintainers.
        s = _sample_state()
        # Smoke: no field literally named *_secret
        for key in s.resource_ids:
            assert "secret" not in key.lower(), (
                f"resource_ids must not contain secret-shaped keys; "
                f"got {key!r}"
            )

    def test_state_file_has_no_secret_keys_after_write(self, tmp_path: Path):
        p = tmp_path / ".bootstrap-state.json"
        write_state(p, _sample_state())
        raw = p.read_text().lower()
        assert "secret" not in raw, "the word 'secret' should not appear in state"
        assert "passphrase" not in raw
        assert "password" not in raw
