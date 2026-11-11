"""Tests for the generated FastAPI middleware: full JWT validation + actor-sub allowlist.

Implements R9 acceptance criteria (8 negative + 1 positive cases) and Invariant 7
(structured logging for every PASS/FAIL decision).

Approach:
  1. Render the Copier template to a tmp dir (`copier_copy` fixture from conftest).
  2. Add the rendered app to sys.path and import `app.auth.middleware`.
  3. Mock the Logto JWKS endpoint via the `responses` library.
  4. Forge JWTs with a controlled EC P-384 keypair (matches ES384 algo used in template).
  5. Build a minimal FastAPI TestClient against a route protected by `validate_token`
     and assert response status + structured-log emission.

Notes:
  - This test injects env vars via monkeypatch into the rendered app's runtime
    BEFORE its `settings` module loads. The rendered `app.config.Settings` reads
    from os.environ via pydantic-settings.
  - Settings are case-INsensitive — `LOGTO_M2M_AGENT_APP_ID` env var sets
    `settings.logto_m2m_agent_app_id`.
  - Actor-sub header name is per-app: `X-{APP_NAME_UPPER_WITH_UNDERSCORES}-Actor-Sub`.
    For app name "test-app" → "X-TEST_APP-Actor-Sub" (hyphens become underscores
    in the uppercase form). The allowlist env var is "TEST_APP_M2M_ACTOR_SUBS".
"""

from __future__ import annotations

import importlib
import json
import sys
import time
import uuid

import jwt
import pytest
import responses
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Constants used across all tests in this module
# ---------------------------------------------------------------------------

APP_NAME = "test-app"
APP_NAME_UPPER = "TEST_APP"  # uppercase, hyphens→underscores
LOGTO_ENDPOINT = "https://auth.example.test"
API_RESOURCE = f"https://{APP_NAME}.example.test/api"
ISSUER = f"{LOGTO_ENDPOINT}/oidc"
JWKS_URL = f"{LOGTO_ENDPOINT}/oidc/jwks"
M2M_APP_ID = "m2m-app-id-correct-12345"
SPA_APP_ID = "spa-app-id-67890"
WRONG_M2M_APP_ID = "m2m-app-id-evil-99999"
REQUIRED_SCOPE = "items:read"

ACTOR_SUB_HEADER = f"X-{APP_NAME_UPPER}-Actor-Sub"
ACTOR_SUB_ALLOWLIST_ENV = f"{APP_NAME_UPPER}_M2M_ACTOR_SUBS"

ALLOWED_ACTOR_SUB = "app_test_app"
UNLISTED_ACTOR_SUB = "intruder-actor"
TOKEN_SUB = "agent-m2m-subject-id"
# A human who logged in through this app's Logto SPA.
USER_SUB = "human-user-subject-id"


# ---------------------------------------------------------------------------
# Keypair + JWKS helpers
# ---------------------------------------------------------------------------


def _new_keypair():
    """Create a fresh EC P-384 keypair (algorithm ES384)."""
    return ec.generate_private_key(ec.SECP384R1())


def _jwk_from_public_key(public_key, kid: str) -> dict:
    """Return the JWK form of an EC P-384 public key (Logto-style)."""
    numbers = public_key.public_numbers()

    # Per RFC 7518 §6.2.1: x and y are big-endian, fixed-length (48 bytes for P-384),
    # base64url-encoded without padding.
    def _b64u(n: int) -> str:
        import base64

        b = n.to_bytes(48, "big")
        return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")

    return {
        "kty": "EC",
        "crv": "P-384",
        "x": _b64u(numbers.x),
        "y": _b64u(numbers.y),
        "use": "sig",
        "alg": "ES384",
        "kid": kid,
    }


def _private_pem(private_key) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _mint_token(
    private_key,
    *,
    kid: str,
    iss: str = ISSUER,
    aud: str = API_RESOURCE,
    client_id: str | None = M2M_APP_ID,
    azp: str | None = None,
    scope: str = REQUIRED_SCOPE,
    sub: str | None = None,
    expires_in: int = 300,
) -> str:
    """Forge an M2M JWT signed by `private_key` with the requested claims.

    `sub` defaults to `client_id` because that is what Logto's
    `client_credentials` grant actually emits, and it is the signal the
    middleware uses to tell an M2M token from a user token. A fixture with
    `sub != client_id` would be silently classified as a user token and would
    skip every M2M check this module exists to verify.
    """
    now = int(time.time())
    if sub is None:
        sub = client_id if client_id is not None else TOKEN_SUB
    payload: dict = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": now,
        "exp": now + expires_in,
        "scope": scope,
    }
    if client_id is not None:
        payload["client_id"] = client_id
    if azp is not None:
        payload["azp"] = azp

    return jwt.encode(
        payload,
        _private_pem(private_key),
        algorithm="ES384",
        headers={"kid": kid, "alg": "ES384"},
    )


def _mint_user_token(
    private_key,
    *,
    kid: str,
    iss: str = ISSUER,
    aud: str = API_RESOURCE,
    scope: str = REQUIRED_SCOPE,
    sub: str = USER_SUB,
    expires_in: int = 300,
) -> str:
    """Forge a JWT shaped like a Logto SPA user token — note: no `client_id`."""
    now = int(time.time())
    return jwt.encode(
        {
            "iss": iss,
            "aud": aud,
            "sub": sub,
            "iat": now,
            "exp": now + expires_in,
            "scope": scope,
            "jti": str(uuid.uuid4()),
        },
        _private_pem(private_key),
        algorithm="ES384",
        headers={"kid": kid, "alg": "ES384"},
    )


# ---------------------------------------------------------------------------
# Fixture: render template + load middleware module from it
# ---------------------------------------------------------------------------


@pytest.fixture
def rendered_middleware(tmp_path, monkeypatch, copier_copy):
    """Render the template and return (module, test_client_factory, log_capture)."""
    # 1. Render the template
    result_dir = copier_copy(
        app_name=APP_NAME,
        include_frontend=False,
        logto_endpoint=LOGTO_ENDPOINT,
    )

    # 2. Set env vars BEFORE importing app.config / app.auth.middleware
    monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)
    monkeypatch.setenv("LOGTO_API_RESOURCE", API_RESOURCE)
    monkeypatch.setenv("LOGTO_M2M_AGENT_APP_ID", M2M_APP_ID)
    # Auth counts as configured when either Logto app ID is set. Set both
    # here so the JWT checks below run; TestAuthNotConfigured clears them.
    monkeypatch.setenv("LOGTO_APP_ID", SPA_APP_ID)
    monkeypatch.setenv(f"{APP_NAME_UPPER}_REQUIRED_SCOPE", REQUIRED_SCOPE)
    # ALLOWLIST_ENV varies per test; tests set it via monkeypatch directly.

    # 3. Insert the rendered app dir at the front of sys.path
    monkeypatch.syspath_prepend(str(result_dir))

    # 4. Force-reload `app` and submodules — pytest may have cached them from a
    #    prior test (different tmp_path) and they would point at the wrong files.
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]

    yield result_dir

    # Cleanup: remove rendered-app modules from cache so the next test gets fresh.
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]


def _build_app_and_capture_logs(monkeypatch):
    """Import rendered middleware, build a tiny FastAPI app, return (client, log_list).

    Captures structured-log records emitted by the middleware for assertion.
    """
    import logging

    middleware_mod = importlib.import_module("app.auth.middleware")
    validate_token = middleware_mod.validate_token

    log_records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
            log_records.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("app.auth.middleware")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # Also attach to root in case the middleware uses a different logger name.
    root = logging.getLogger()
    root.addHandler(handler)

    app = FastAPI()

    @app.get("/protected")
    def protected(_claims: dict = Depends(validate_token)):
        return {"ok": True}

    client = TestClient(app, raise_server_exceptions=False)
    return client, log_records, middleware_mod


# ---------------------------------------------------------------------------
# Mock JWKS endpoint
# ---------------------------------------------------------------------------


def _serve_jwks(rsps: responses.RequestsMock, public_key, kid: str):
    """Register a stubbed Logto JWKS endpoint."""
    rsps.add(
        responses.GET,
        JWKS_URL,
        json={"keys": [_jwk_from_public_key(public_key, kid)]},
        status=200,
    )


# ---------------------------------------------------------------------------
# Tests — one positive, eight negative (R9 acceptance criteria)
# ---------------------------------------------------------------------------


class TestActorSubAndJwtValidation:
    """R9 + Invariant 7: full JWT validation + actor-sub allowlist + structured logs."""

    def test_accepts_valid_token_with_allowed_actor(self, rendered_middleware, monkeypatch):
        """Positive case: every check passes → 200 + log line with decision=200."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-1"
        token = _mint_token(priv, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert r.status_code == 200, r.text
        # Structured log assertion
        decision_logs = [ln for ln in logs if "configured_app_slug" in ln]
        assert decision_logs, f"no structured log emitted; logs={logs}"
        record = _parse_json_in_log(decision_logs[-1])
        assert record["decision"].startswith("200"), record
        assert record["aud"] == API_RESOURCE
        # On the M2M path `sub` is rewritten to the delegated actor, so writes
        # land under the human's identity rather than the robot's — this is
        # the contract AGENT_CONTEXT.md advertises to CLI callers.
        assert record["sub"] == ALLOWED_ACTOR_SUB
        assert record["x_actor_sub_header"] == ALLOWED_ACTOR_SUB
        assert record["configured_app_slug"] == APP_NAME

    def test_rejects_missing_actor_sub_header(self, rendered_middleware, monkeypatch):
        """R9: valid JWT but no X-{App}-Actor-Sub header → 403 fail-closed."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-2"
        token = _mint_token(priv, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

        assert r.status_code == 403, r.text
        record = _last_structured_log(logs)
        assert record is not None, f"no structured log; logs={logs}"
        assert record["decision"].startswith("403")
        assert record["x_actor_sub_header"] in (None, "", "<missing>"), record

    def test_rejects_unallowed_actor_sub(self, rendered_middleware, monkeypatch):
        """Valid JWT + header present but NOT in allowlist → 403."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-3"
        token = _mint_token(priv, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: UNLISTED_ACTOR_SUB,
                },
            )

        assert r.status_code == 403
        record = _last_structured_log(logs)
        assert record and record["decision"].startswith("403"), record
        assert record["x_actor_sub_header"] == UNLISTED_ACTOR_SUB

    def test_rejects_wrong_iss(self, rendered_middleware, monkeypatch):
        """Token with iss=evil.com → 401 (pinned per implementation)."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-4"
        token = _mint_token(priv, kid=kid, iss="https://evil.example.com/oidc")

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert r.status_code == 401
        record = _last_structured_log(logs)
        assert record is not None
        assert record["decision"].split(":", 1)[0] == "401"

    def test_rejects_wrong_audience(self, rendered_middleware, monkeypatch):
        """Token with aud=other-app → 401 (pinned per implementation)."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-5"
        token = _mint_token(priv, kid=kid, aud="https://other-app.example.test/api")

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert r.status_code == 401

    def test_rejects_wrong_client_id(self, rendered_middleware, monkeypatch):
        """Correct aud but client_id/azp ≠ configured m2m_app_id → 403."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-6"
        token = _mint_token(
            priv, kid=kid, client_id=WRONG_M2M_APP_ID, azp=None
        )

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert r.status_code == 403
        record = _last_structured_log(logs)
        assert record is not None
        assert record["decision"].split(":", 1)[0] == "403"

    def test_rejects_expired_token(self, rendered_middleware, monkeypatch):
        """exp in the past → 401."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-7"
        token = _mint_token(priv, kid=kid, expires_in=-60)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert r.status_code == 401

    def test_rejects_missing_scope(self, rendered_middleware, monkeypatch):
        """Token without required scope → 403."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-8"
        token = _mint_token(priv, kid=kid, scope="some:other:scope")

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert r.status_code == 403

    def test_rejects_invalid_signature(self, rendered_middleware, monkeypatch):
        """Token signed with a key the JWKS endpoint doesn't list → 401."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        legit_key = _new_keypair()
        evil_key = _new_keypair()
        kid = "kid-9"
        # Token signed by evil_key, but JWKS only serves legit_key for that kid.
        token = _mint_token(evil_key, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, legit_key.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert r.status_code == 401

    def test_fails_closed_when_m2m_app_id_unconfigured(
        self, rendered_middleware, monkeypatch
    ):
        """Operator misconfig: LOGTO_M2M_AGENT_APP_ID unset → 503 fail-closed.

        Even with an otherwise-valid token + correct actor-sub header, the
        middleware must NOT accept the request if `expected_m2m` is empty —
        a silent skip there would defeat Invariant 2 (token-app binding).
        """
        # Override to empty string; pydantic-settings reads from os.environ.
        monkeypatch.setenv("LOGTO_M2M_AGENT_APP_ID", "")
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-misconfig"
        token = _mint_token(priv, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert r.status_code == 503, (
            "expected 503 fail-closed when LOGTO_M2M_AGENT_APP_ID is empty; "
            f"got {r.status_code}"
        )
        record = _last_structured_log(logs)
        assert record is not None
        assert record["decision"].split(":", 1)[0] == "503"
        assert "m2m_app_id_unconfigured" in record["decision"]


# ---------------------------------------------------------------------------
# Log helpers — middleware emits JSON; tolerate "fields embedded in message"
# ---------------------------------------------------------------------------


def _parse_json_in_log(line: str) -> dict:
    """Find the {...} JSON object inside a log line and parse it."""
    start = line.find("{")
    end = line.rfind("}")
    if start == -1 or end == -1:
        raise AssertionError(f"no JSON object in log line: {line!r}")
    return json.loads(line[start : end + 1])


def _last_structured_log(logs: list[str]) -> dict | None:
    """Return the parsed JSON of the most recent structured log, or None."""
    for line in reversed(logs):
        if "configured_app_slug" in line:
            try:
                return _parse_json_in_log(line)
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# User tokens (R9, revised) — the generated app must still serve human logins.
#
# The template ships a Logto SPA, a LoginPage and a CallbackPage. Between
# 133e582 and this commit the middleware accepted only M2M tokens, so every
# generated app rendered a login screen whose tokens the server then rejected.
# Nothing caught it because every test in this module minted an M2M token.
# ---------------------------------------------------------------------------


class TestUserTokens:
    """A Logto SPA user token must authenticate without any M2M ceremony."""

    def test_accepts_user_token_without_actor_sub_header(
        self, rendered_middleware, monkeypatch
    ):
        """The case the M2M-only middleware got wrong: a plain browser login."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-user-1"
        token = _mint_user_token(priv, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200, resp.text
        assert any('"decision": "200:ok_user"' in line for line in logs)

    def test_user_token_ignores_actor_sub_header(self, rendered_middleware, monkeypatch):
        """Self-impersonation guard.

        A logged-in user sends an allowlisted actor-sub header. If the M2M
        delegation path were applied to user tokens, `sub` would be rewritten
        and that user would become someone else purely by setting a header.
        """
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-user-2"
        token = _mint_user_token(priv, kid=kid, sub=USER_SUB)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, logs, mod = _build_app_and_capture_logs(monkeypatch)
            resp = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert resp.status_code == 200, resp.text
        # The decision log records the real user, not the header value.
        decision = next(json.loads(line) for line in logs if '"decision"' in line)
        assert decision["sub"] == USER_SUB
        assert decision["decision"] == "200:ok_user"

    def test_user_token_unaffected_by_unconfigured_m2m_app_id(
        self, rendered_middleware, monkeypatch
    ):
        """Human login must not depend on M2M configuration being present.

        An app deployed with no agent access at all should still let people in.
        """
        monkeypatch.setenv("LOGTO_M2M_AGENT_APP_ID", "")
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, "")

        priv = _new_keypair()
        kid = "kid-user-3"
        token = _mint_user_token(priv, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, _logs, _ = _build_app_and_capture_logs(monkeypatch)
            resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200, resp.text

    def test_user_token_still_needs_required_scope(self, rendered_middleware, monkeypatch):
        """Scope enforcement applies to both token kinds."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-user-4"
        token = _mint_user_token(priv, kid=kid, scope="some:other:scope")

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, _logs, _ = _build_app_and_capture_logs(monkeypatch)
            resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 403

    def test_user_token_still_needs_correct_audience(self, rendered_middleware, monkeypatch):
        """Invariant 2 holds for user tokens too — no cross-app token reuse."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "kid-user-5"
        token = _mint_user_token(priv, kid=kid, aud="https://other-app.example.test/api")

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, _logs, _ = _build_app_and_capture_logs(monkeypatch)
            resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 401


class TestTokenKindDiscriminator:
    """The `sub == client_id` heuristic, tested directly."""

    def test_classification(self, rendered_middleware, monkeypatch):
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)
        _client, _logs, mod = _build_app_and_capture_logs(monkeypatch)

        m2m = {"sub": M2M_APP_ID, "client_id": M2M_APP_ID}
        user = {"sub": USER_SUB}
        # Guards the case where Logto starts emitting client_id on user tokens:
        # a bare `"client_id" in claims` check would misroute this and break login.
        user_with_client_id = {"sub": USER_SUB, "client_id": "the-spa-app-id"}

        assert mod._is_m2m_token(m2m) is True
        assert mod._is_m2m_token(user) is False
        assert mod._is_m2m_token(user_with_client_id) is False


# ---------------------------------------------------------------------------
# No-auth lockout (R11, KTD7) — a deployment with no Logto app returns no data.
# ---------------------------------------------------------------------------


def _unsigned_token(kid: str = "kid-unsigned") -> str:
    """A well-formed JWT whose signature segment is junk (no key signed it)."""
    import base64

    def _b64u(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    now = int(time.time())
    header = _b64u({"alg": "ES384", "kid": kid, "typ": "JWT"})
    payload = _b64u(
        {
            "iss": ISSUER,
            "aud": API_RESOURCE,
            "sub": USER_SUB,
            "iat": now,
            "exp": now + 300,
            "scope": REQUIRED_SCOPE,
        }
    )
    return f"{header}.{payload}.AAAA"


class TestAuthNotConfigured:
    """Both Logto app IDs empty → every protected request is a 503, whatever the token."""

    @pytest.fixture(autouse=True)
    def _no_logto_apps(self, rendered_middleware, monkeypatch):
        monkeypatch.setenv("LOGTO_APP_ID", "")
        monkeypatch.setenv("LOGTO_M2M_AGENT_APP_ID", "")
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

    def test_no_token_returns_503_auth_not_configured(self, monkeypatch):
        with responses.RequestsMock() as rsps:
            client, logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get("/protected")
            assert len(rsps.calls) == 0

        assert r.status_code == 503, r.text
        assert r.json() == {"detail": "auth_not_configured"}
        record = _last_structured_log(logs)
        assert record is not None
        assert record["decision"] == "503:auth_not_configured"

    def test_unsigned_token_returns_503_without_jwks_fetch(self, monkeypatch):
        # No JWKS stub is registered, so any fetch would raise inside the
        # mock and surface as a 500. A 503 plus zero calls proves the check
        # runs before the decode.
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            client, _logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get(
                "/protected",
                headers={"Authorization": f"Bearer {_unsigned_token()}"},
            )
            assert len(rsps.calls) == 0

        assert r.status_code == 503, r.text
        assert r.json() == {"detail": "auth_not_configured"}

    def test_valid_signed_token_still_returns_503(self, monkeypatch):
        """A token that would otherwise pass every check still gets no data."""
        priv = _new_keypair()
        kid = "kid-noauth"
        token = _mint_user_token(priv, kid=kid)

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client, _logs, _ = _build_app_and_capture_logs(monkeypatch)
            r = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
            assert len(rsps.calls) == 0

        assert r.status_code == 503, r.text
        assert r.json() == {"detail": "auth_not_configured"}

    @pytest.mark.parametrize(
        ("app_id", "m2m_id"),
        [(SPA_APP_ID, ""), ("", M2M_APP_ID), (SPA_APP_ID, M2M_APP_ID)],
        ids=["spa-only", "m2m-only", "both"],
    )
    def test_either_id_set_keeps_bearer_check(self, monkeypatch, app_id, m2m_id):
        """With at least one app ID the existing 401 for a missing bearer stays."""
        monkeypatch.setenv("LOGTO_APP_ID", app_id)
        monkeypatch.setenv("LOGTO_M2M_AGENT_APP_ID", m2m_id)

        client, _logs, _ = _build_app_and_capture_logs(monkeypatch)
        r = client.get("/protected")

        assert r.status_code == 401, r.text
        assert r.json() == {"detail": "missing_bearer"}
