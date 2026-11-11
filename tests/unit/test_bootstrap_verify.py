"""Unit tests for :mod:`agentic_core.bootstrap.verify` (U7 / R5 / R8).

``verify_deployment`` is the final gate: it polls the deployed ``/health``
until 200 (bounded, transient) and then makes ONE authenticated request that
must be accepted (not 401). The core of this unit is the transient-vs-structural
boundary (KTD6):

  - health never 200 within the bound / network error → TRANSIENT
  - auth call returns 401 (token rejected) → STRUCTURAL

The module is kept pure/injectable: an HTTP client (``.get``/``.post`` seam)
and a ``mint_token`` callable are injected so these tests never touch the
network.
"""

from __future__ import annotations

import pytest

from agentic_core.bootstrap import verify


# ---------------------------------------------------------------------------
# Injectable fakes
# ---------------------------------------------------------------------------


class FakeResp:
    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


class FakeClient:
    """A minimal httpx.Client stand-in with a scripted response queue."""

    def __init__(self, get_responses=None, post_response=None):
        # Each entry is a FakeResp or an Exception (raised to simulate a
        # network error). Consumed in order across GET calls.
        self._get = list(get_responses or [])
        self.post_response = post_response
        self.get_calls: list[tuple] = []
        self.post_calls: list[tuple] = []
        self.closed = False

    def get(self, url, headers=None):
        self.get_calls.append((url, headers))
        item = self._get.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, data=None, headers=None):
        self.post_calls.append((url, data, headers))
        if isinstance(self.post_response, Exception):
            raise self.post_response
        return self.post_response

    def close(self):
        self.closed = True


def _no_sleep(_seconds):  # deterministic, instant tests
    return None


# ---------------------------------------------------------------------------
# Success: /health 200 + auth accepted
# ---------------------------------------------------------------------------


def test_health_200_and_auth_accepted_is_success():
    client = FakeClient(get_responses=[FakeResp(200), FakeResp(403)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok-123",
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.SUCCESS
    assert result.get("auth_checked") is True
    # health GET then one authenticated GET (Bearer token present).
    assert len(client.get_calls) == 2
    health_url, _ = client.get_calls[0]
    auth_url, auth_headers = client.get_calls[1]
    assert health_url == "https://foo.up.railway.app/health"
    assert auth_headers["Authorization"] == "Bearer tok-123"


def test_200_auth_status_is_accepted():
    """A 200 (token accepted + route ok) is auth success."""
    client = FakeClient(get_responses=[FakeResp(200), FakeResp(200)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.SUCCESS
    assert result.get("auth_checked") is True


def test_auth_403_is_accepted():
    """403 = JWT VALID but actor-sub not allowlisted. The JWT WAS validated, so
    auth works — this must count as success, not a failure."""
    client = FakeClient(get_responses=[FakeResp(200), FakeResp(403)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.SUCCESS
    assert result.get("auth_checked") is True


def test_auth_503_is_structural():
    """THE BUG: 503 = the deployed middleware fails closed because its M2M config
    (LOGTO_M2M_AGENT_APP_ID) is unset. Auth infra is broken — a redeploy/config
    fix is needed, resume won't help — so it is STRUCTURAL, never a rubber-stamped
    success."""
    client = FakeClient(get_responses=[FakeResp(200), FakeResp(503)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.STRUCTURAL
    assert result["error_code"] == "VERIFY_AUTH_MISCONFIGURED"


def test_auth_500_is_transient():
    """A 5xx (other than 503) is a slow rollout / transient server error →
    retryable."""
    client = FakeClient(get_responses=[FakeResp(200), FakeResp(500)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.TRANSIENT
    assert result["error_code"] == "VERIFY_AUTH_UNAVAILABLE"


def test_auth_429_is_transient():
    """429 rate-limit is transient → retryable."""
    client = FakeClient(get_responses=[FakeResp(200), FakeResp(429)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.TRANSIENT
    assert result["error_code"] == "VERIFY_AUTH_UNAVAILABLE"


def test_auth_404_is_structural():
    """404 = wrong route / probe path — not proven, and re-running can't fix a
    wrong probe → STRUCTURAL."""
    client = FakeClient(get_responses=[FakeResp(200), FakeResp(404)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.STRUCTURAL
    assert result["error_code"] == "VERIFY_AUTH_ROUTE_MISSING"


# ---------------------------------------------------------------------------
# Transient: health 200 only after a couple polls (still succeeds in bound)
# ---------------------------------------------------------------------------


def test_health_becomes_200_after_polls_still_succeeds():
    client = FakeClient(
        get_responses=[FakeResp(503), FakeResp(500), FakeResp(200), FakeResp(403)]
    )
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        max_health_attempts=5,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.SUCCESS
    # 3 health polls (503, 500, 200) + 1 auth GET.
    assert len(client.get_calls) == 4


# ---------------------------------------------------------------------------
# Transient: health never 200 within the bound → TRANSIENT (retryable upstream)
# ---------------------------------------------------------------------------


def test_health_never_200_within_bound_is_transient():
    client = FakeClient(get_responses=[FakeResp(500), FakeResp(500), FakeResp(500)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        max_health_attempts=3,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.TRANSIENT
    assert result["error_code"] == "VERIFY_HEALTH_UNAVAILABLE"
    # Exactly the bounded number of polls; no auth attempt after exhaustion.
    assert len(client.get_calls) == 3


def test_default_health_budget_tolerates_multi_minute_rollout():
    """Finding 4b: ``railway up -d`` is DETACHED and a Dockerfile build+rollout
    takes several minutes, so the DEFAULT health-poll budget must span roughly
    ~5 minutes of bounded wait (health-exhausted is retryable, so over-waiting
    only costs time). Uses an injected sleep to keep the test instant."""
    slept: list[float] = []
    client = FakeClient(
        get_responses=[FakeResp(500)] * verify.DEFAULT_MAX_HEALTH_ATTEMPTS
    )
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        sleep=lambda s: slept.append(s),
    )
    assert result["status"] == verify.TRANSIENT
    assert result["error_code"] == "VERIFY_HEALTH_UNAVAILABLE"
    # Generous default: enough polls and cumulative backoff for a real rollout.
    assert verify.DEFAULT_MAX_HEALTH_ATTEMPTS >= 15
    assert sum(slept) >= 250, (
        f"default bounded wait {sum(slept)}s is too short for a detached "
        "multi-minute Railway rollout"
    )


def test_health_network_error_within_bound_is_transient():
    client = FakeClient(
        get_responses=[ConnectionError("refused"), ConnectionError("refused")]
    )
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        max_health_attempts=2,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.TRANSIENT


# ---------------------------------------------------------------------------
# Structural: auth token rejected (401) → STRUCTURAL (non-retryable upstream)
# ---------------------------------------------------------------------------


def test_auth_network_error_is_transient():
    """/health returns 200 but the subsequent AUTHENTICATED GET raises a
    network/connection error → TRANSIENT with VERIFY_AUTH_NETWORK_ERROR (not
    STRUCTURAL, not a crash). Guards the distinct try/except around the auth
    probe request (verify.py auth-network branch)."""
    client = FakeClient(
        get_responses=[FakeResp(200), ConnectionError("connection reset")]
    )
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.TRANSIENT
    assert result["error_code"] == "VERIFY_AUTH_NETWORK_ERROR"
    # health GET succeeded, auth GET attempted (and raised).
    assert len(client.get_calls) == 2


def test_auth_401_is_structural():
    client = FakeClient(get_responses=[FakeResp(200), FakeResp(401)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=lambda: "tok",
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.STRUCTURAL
    assert result["error_code"] == "VERIFY_AUTH_REJECTED"


# ---------------------------------------------------------------------------
# Token mint failure → TRANSIENT (auth infra / cold cache, re-runnable)
# ---------------------------------------------------------------------------


def test_token_mint_failure_is_transient():
    def _boom():
        raise verify.VerifyError("mint failed")

    client = FakeClient(get_responses=[FakeResp(200)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=_boom,
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.TRANSIENT
    assert result["error_code"] == "VERIFY_TOKEN_MINT_FAILED"


# ---------------------------------------------------------------------------
# Health-only mode: mint_token None skips the auth probe
# ---------------------------------------------------------------------------


def test_no_mint_token_does_health_only():
    client = FakeClient(get_responses=[FakeResp(200)])
    result = verify.verify_deployment(
        "https://foo.up.railway.app",
        mint_token=None,
        http_client=client,
        sleep=_no_sleep,
    )
    assert result["status"] == verify.SUCCESS
    assert result.get("auth_checked") is False
    # Only the health GET — no auth call attempted.
    assert len(client.get_calls) == 1


# ---------------------------------------------------------------------------
# make_m2m_token_minter builds a client-credentials minter
# ---------------------------------------------------------------------------


def test_make_m2m_token_minter_posts_client_credentials():
    client = FakeClient(post_response=FakeResp(200, {"access_token": "app-tok"}))
    mint = verify.make_m2m_token_minter(
        endpoint="https://auth.example.dev",
        client_id="m2m-id",
        client_secret="m2m-secret",
        resource="https://foo.example.test/api",
        http_client=client,
    )
    token = mint()
    assert token == "app-tok"
    url, data, _headers = client.post_calls[0]
    assert url == "https://auth.example.dev/oidc/token"
    assert data["grant_type"] == "client_credentials"
    assert data["client_id"] == "m2m-id"
    assert data["client_secret"] == "m2m-secret"
    assert data["resource"] == "https://foo.example.test/api"


def test_make_m2m_token_minter_raises_on_http_error():
    client = FakeClient(post_response=FakeResp(401, {"error": "invalid_client"}))
    mint = verify.make_m2m_token_minter(
        endpoint="https://auth.example.dev",
        client_id="m2m-id",
        client_secret="bad-secret",
        resource="https://foo.example.test/api",
        http_client=client,
    )
    with pytest.raises(verify.VerifyError):
        mint()
