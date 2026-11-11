"""Integration test — generated app middleware enforces actor-sub fail-closed (task #18).

Renders the full-variant template via the in-process Copier wrapper, then
imports the rendered ``app.auth.middleware`` and exercises it against a
FastAPI ``TestClient`` with real ES384-signed JWTs and a stubbed Logto JWKS
endpoint (``responses`` library).

Scope of this integration test (focused subset of R9 acceptance criteria;
the broader 8 negative + 1 positive cases live in
``tests/unit/test_template_middleware_actor_sub.py``):

  1. Missing ``X-{App}-Actor-Sub`` header → 403 fail-closed (R9).
  2. Valid actor-sub in allowlist → 200.
  3. Actor-sub present but NOT in allowlist → 403.

Why a separate integration test (not just the unit test)? The unit test
uses the ``copier_copy`` subprocess fixture; this one drives the
in-process ``render_template`` wrapper (Task #6, PERF-004) so the
template → import → request path is exercised end-to-end through the
same code path the bootstrap orchestrator uses in production. If the
in-process wrapper ever desyncs from the subprocess one (different Copier
options, different output tree), this test catches it.

Marked ``@pytest.mark.slow`` per task #18 spec (renders template +
generates fresh EC P-384 keypair per test). Pytest emits an unknown-marker
warning but runs the tests regardless; the marker is a filter knob for
humans (``pytest -m "not slow"`` skips, ``pytest -m slow`` runs only
slow). The marker is intentionally not registered in conftest.py to
avoid touching shared test config in this scoped task.
"""

from __future__ import annotations

import base64
import importlib
import json
import sys
import time
from pathlib import Path

import jwt
import pytest
import responses
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from agentic_core.bootstrap import template as template_mod


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"


# ---------------------------------------------------------------------------
# Per-app constants. App name → header / env-var derivations mirror the
# middleware template's ``APP_UPPER = APP_SLUG.replace('-', '_').upper()``.
# ---------------------------------------------------------------------------

APP_NAME = "mw-integ-app"
APP_NAME_UPPER = "MW_INTEG_APP"

LOGTO_ENDPOINT = "https://logto.integ.example"
API_RESOURCE = f"https://api.{APP_NAME}.example"
ISSUER = f"{LOGTO_ENDPOINT}/oidc"
JWKS_URL = f"{LOGTO_ENDPOINT}/oidc/jwks"
M2M_APP_ID = "integ-m2m-app-id-aaa"
REQUIRED_SCOPE = f"{APP_NAME}:api"  # middleware default

ACTOR_SUB_HEADER = f"X-{APP_NAME_UPPER}-Actor-Sub"
ACTOR_SUB_ALLOWLIST_ENV = f"{APP_NAME_UPPER}_M2M_ACTOR_SUBS"
REQUIRED_SCOPE_ENV = f"{APP_NAME_UPPER}_REQUIRED_SCOPE"

ALLOWED_ACTOR_SUB = "integ-allowed-actor"
UNLISTED_ACTOR_SUB = "integ-intruder"
TOKEN_SUB = "integ-m2m-subject"


# ---------------------------------------------------------------------------
# JWT minting helpers — EC P-384 keypair, ES384 signature
# ---------------------------------------------------------------------------


def _new_keypair():
    """Fresh EC P-384 keypair (matches the ES384 algorithm Logto uses)."""
    return ec.generate_private_key(ec.SECP384R1())


def _jwk_from_public_key(public_key, kid: str) -> dict:
    """Return JWK form of an EC P-384 public key (RFC 7518 §6.2.1)."""
    numbers = public_key.public_numbers()

    def _b64u(n: int) -> str:
        # P-384 → 48-byte big-endian coordinates, base64url, no padding.
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
    """Sign a JWT with ``private_key`` and the requested claims."""
    now = int(time.time())
    payload: dict = {
        "iss": iss,
        "aud": aud,
        # Logto's client_credentials grant sets sub == client_id, and the
        # middleware uses that equality to tell M2M tokens from user tokens.
        # A fixture with sub != client_id would be classified as a user token
        # and would skip every M2M check this file exists to verify.
        "sub": sub if sub is not None else (client_id if client_id is not None else TOKEN_SUB),
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


def _serve_jwks(rsps: responses.RequestsMock, public_key, kid: str) -> None:
    """Stub the Logto JWKS endpoint with a single key."""
    rsps.add(
        responses.GET,
        JWKS_URL,
        json={"keys": [_jwk_from_public_key(public_key, kid)]},
        status=200,
    )


# ---------------------------------------------------------------------------
# Fixture: render template, import middleware, hand back a TestClient factory
# ---------------------------------------------------------------------------


@pytest.fixture
def rendered_middleware(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Render template, prep env, import ``app.auth.middleware``, return module.

    Per-test (not module-scoped) so the JWKS cache + env vars stay isolated.
    The render is sub-second via the in-process Copier wrapper (PERF-004).
    """
    out = tmp_path / "out"
    out.mkdir()
    template_mod.render_template(
        template_dir=TEMPLATE_DIR,
        output_dir=out,
        data={
            "app_name": APP_NAME,
            "app_description": "Integration middleware test app",
            "include_frontend": False,
            "include_mobile": False,
            "github_user": "testuser",
            "logto_endpoint": LOGTO_ENDPOINT,
            "with_auth": True,
        },
        variant="full",
    )
    app_dir = out / APP_NAME
    assert (app_dir / "app" / "auth" / "middleware.py").exists(), (
        "render_template did not produce app/auth/middleware.py"
    )

    # Pre-set settings env vars BEFORE importing app.config / app.auth.middleware.
    # Settings reads via pydantic-settings; APP_SLUG / APP_UPPER are baked
    # into the rendered middleware at template-render time, but the
    # LOGTO_* / M2M_AGENT_APP_ID / ALLOWLIST values come from env at import.
    monkeypatch.setenv("LOGTO_ENDPOINT", LOGTO_ENDPOINT)
    monkeypatch.setenv("LOGTO_API_RESOURCE", API_RESOURCE)
    monkeypatch.setenv("LOGTO_M2M_AGENT_APP_ID", M2M_APP_ID)
    monkeypatch.setenv(REQUIRED_SCOPE_ENV, REQUIRED_SCOPE)
    # Allowlist defaults to empty; each test sets it explicitly via monkeypatch
    # so the "not in allowlist" branch can be exercised without env bleed.

    # Strip any pre-existing `app` modules from a prior test (other tests
    # in the suite render different apps under the same `app` package name).
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]

    monkeypatch.syspath_prepend(str(app_dir))

    middleware_mod = importlib.import_module("app.auth.middleware")

    # JWKS cache is process-global; reset before each test so a stale
    # entry from another test doesn't satisfy our `_get_signing_key` call
    # without going through the `responses` mock.
    middleware_mod._reset_jwks_cache_for_tests()

    yield middleware_mod

    # Tear down rendered-app modules so the next test gets a fresh import.
    for mod_name in list(sys.modules):
        if mod_name == "app" or mod_name.startswith("app."):
            del sys.modules[mod_name]


def _build_client(middleware_mod) -> TestClient:
    """Build a minimal FastAPI app with one route protected by the middleware."""
    validate_token = middleware_mod.validate_token

    app = FastAPI()

    @app.get("/protected")
    def protected(_claims: dict = Depends(validate_token)):
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Tests — R9 fail-closed + actor-sub allowlist subset (3 cases)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestActorSubFailClosed:
    """R9: missing actor-sub fails closed; allowlist gating works both ways."""

    def test_missing_actor_sub_header_returns_403(
        self, rendered_middleware, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R9: valid JWT but no X-{App}-Actor-Sub → 403 (fail-closed)."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "mw-kid-missing"
        token = _mint_token(priv, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client = _build_client(rendered_middleware)
            r = client.get(
                "/protected",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert r.status_code == 403, (
            f"missing X-{APP_NAME_UPPER}-Actor-Sub must yield 403 (R9 fail-closed); "
            f"got {r.status_code} body={r.text}"
        )

    def test_valid_actor_sub_in_allowlist_returns_200(
        self, rendered_middleware, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid JWT + actor-sub present in allowlist → 200."""
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "mw-kid-allowed"
        token = _mint_token(priv, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client = _build_client(rendered_middleware)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                },
            )

        assert r.status_code == 200, (
            f"valid JWT + allowed actor-sub must yield 200; "
            f"got {r.status_code} body={r.text}"
        )
        assert r.json() == {"ok": True}

    def test_actor_sub_not_in_allowlist_returns_403(
        self, rendered_middleware, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Valid JWT + actor-sub header present but NOT in allowlist → 403."""
        # Allowlist contains only ALLOWED_ACTOR_SUB; we send UNLISTED_ACTOR_SUB.
        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        priv = _new_keypair()
        kid = "mw-kid-unlisted"
        token = _mint_token(priv, kid=kid)

        with responses.RequestsMock() as rsps:
            _serve_jwks(rsps, priv.public_key(), kid)
            client = _build_client(rendered_middleware)
            r = client.get(
                "/protected",
                headers={
                    "Authorization": f"Bearer {token}",
                    ACTOR_SUB_HEADER: UNLISTED_ACTOR_SUB,
                },
            )

        assert r.status_code == 403, (
            f"actor-sub not in allowlist must yield 403; "
            f"got {r.status_code} body={r.text}"
        )

    def test_structured_log_emitted_on_decision(
        self, rendered_middleware, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invariant 7: every decision emits a structured JSON log line.

        Belt-and-braces around the 3 status-code assertions above — verifies
        the audit-trail side of R9 (so the cross-app reuse-detection cron
        has data to ingest).
        """
        import logging

        monkeypatch.setenv(ACTOR_SUB_ALLOWLIST_ENV, ALLOWED_ACTOR_SUB)

        log_records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_records.append(self.format(record))

        handler = _Capture()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("app.auth.middleware")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            priv = _new_keypair()
            kid = "mw-kid-log"
            token = _mint_token(priv, kid=kid)

            with responses.RequestsMock() as rsps:
                _serve_jwks(rsps, priv.public_key(), kid)
                client = _build_client(rendered_middleware)
                client.get(
                    "/protected",
                    headers={
                        "Authorization": f"Bearer {token}",
                        ACTOR_SUB_HEADER: ALLOWED_ACTOR_SUB,
                    },
                )
        finally:
            logger.removeHandler(handler)

        # Find the structured log line (contains `configured_app_slug`).
        structured = [line for line in log_records if "configured_app_slug" in line]
        assert structured, (
            f"middleware must emit a structured log line per Invariant 7; "
            f"got records: {log_records}"
        )
        # Extract the JSON object embedded in the log line.
        line = structured[-1]
        start, end = line.find("{"), line.rfind("}")
        record = json.loads(line[start : end + 1])
        assert record["configured_app_slug"] == APP_NAME
        assert record["decision"].startswith("200"), record
