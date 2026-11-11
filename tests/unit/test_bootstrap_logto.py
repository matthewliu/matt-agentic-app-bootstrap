"""Unit tests for agentic_core.bootstrap.logto — Management API client.

Covers:
  - R6: server-side search via ?search=, exponential backoff on 429,
        management token caching at ~/.cache/agentic-core/mgmt-token.json
  - Per-app provisioning produces distinct SPA / M2M / resource / user
  - admin_credential parameter (bytearray) is used as M2M client_secret
"""

from __future__ import annotations

import json
import os
import stat
import time

import pytest
import responses
from responses import matchers

from agentic_core.bootstrap import logto


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

LOGTO_ENDPOINT = "https://logto.test.example.com"
MGMT_RESOURCE = "https://default.logto.app/api"
ADMIN_M2M_ID = "admin-m2m-id"
ADMIN_M2M_SECRET = "admin-m2m-secret-998877"


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Redirect the mgmt-token cache to tmp_path so tests never touch ~/.cache."""
    cache_dir = tmp_path / "cache" / "agentic-core"
    monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_dir / "mgmt-token.json")
    # Make backoff fast so 429-retry tests don't take 30+ seconds.
    monkeypatch.setattr(logto, "BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(logto, "BACKOFF_MAX_SECONDS", 0.0)
    monkeypatch.setattr(logto, "BACKOFF_JITTER_FRACTION", 0.0)
    yield


def _admin_cred() -> bytearray:
    """Build the bytearray admin credential parameter (keystore stub)."""
    return bytearray(ADMIN_M2M_SECRET, "utf-8")


def _stub_token_endpoint(access_token: str = "mgmt-token-xyz", expires_in: int = 3600) -> None:
    """Stub the OIDC token endpoint with a single response."""
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/oidc/token",
        json={"access_token": access_token, "expires_in": expires_in, "token_type": "Bearer"},
        status=200,
    )


# ---------------------------------------------------------------------------
# RateLimitError + exponential backoff (R6)
# ---------------------------------------------------------------------------


class TestRateLimitBackoff:
    """429 → exponential backoff with jitter → RateLimitError after retries."""

    def test_rate_limit_error_class_exists(self):
        assert issubclass(logto.RateLimitError, Exception)

    @responses.activate
    def test_429_triggers_retry_then_raises_after_max_attempts(self, monkeypatch):
        """5 consecutive 429s should raise RateLimitError, not return."""
        _stub_token_endpoint()
        for _ in range(5):
            responses.add(
                responses.GET,
                f"{LOGTO_ENDPOINT}/api/applications",
                json={"error": "Too Many Requests"},
                status=429,
            )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )

        sleeps: list[float] = []
        monkeypatch.setattr(logto.time, "sleep", lambda s: sleeps.append(s))

        with pytest.raises(logto.RateLimitError):
            client.find_application("foo-frontend")

        # One token mint + 5 GET retries = 6 calls
        assert len(responses.calls) == 6
        # At least one sleep call between retries (backoff base is 0 in this test
        # but the call must still happen).
        assert len(sleeps) >= 4

    @responses.activate
    def test_429_then_success_returns_normally(self):
        """One 429 followed by a 200 should retry once and return."""
        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={"error": "rate limited"},
            status=429,
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.find_application("does-not-exist")
        assert result is None

    @responses.activate
    def test_429_honors_retry_after_numeric_seconds(self, monkeypatch):
        """If 429 carries ``Retry-After: 5``, sleep ≈5s, not the formulaic 2s."""
        # Restore the original (non-zero) backoff knobs so we can prove
        # Retry-After is taking precedence over _compute_backoff(attempt).
        monkeypatch.setattr(logto, "BACKOFF_BASE_SECONDS", 2.0)
        monkeypatch.setattr(logto, "BACKOFF_MAX_SECONDS", 32.0)
        monkeypatch.setattr(logto, "BACKOFF_JITTER_FRACTION", 0.0)

        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={"error": "rate limited"},
            status=429,
            headers={"Retry-After": "5"},
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
        )

        sleeps: list[float] = []
        monkeypatch.setattr(logto.time, "sleep", lambda s: sleeps.append(s))

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        client.find_application("does-not-exist")

        # Exactly one sleep (one retry).
        assert len(sleeps) == 1
        # Server said 5s. We honored it (not the formulaic 2s).
        assert sleeps[0] == pytest.approx(5.0, abs=0.01)

    @responses.activate
    def test_429_retry_after_clamped_to_max(self, monkeypatch):
        """Retry-After larger than BACKOFF_MAX_SECONDS is clamped."""
        monkeypatch.setattr(logto, "BACKOFF_BASE_SECONDS", 2.0)
        monkeypatch.setattr(logto, "BACKOFF_MAX_SECONDS", 32.0)
        monkeypatch.setattr(logto, "BACKOFF_JITTER_FRACTION", 0.0)

        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={"error": "rate limited"},
            status=429,
            headers={"Retry-After": "999999"},  # hostile / misconfigured
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
        )

        sleeps: list[float] = []
        monkeypatch.setattr(logto.time, "sleep", lambda s: sleeps.append(s))

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        client.find_application("does-not-exist")

        assert len(sleeps) == 1
        assert sleeps[0] == 32.0  # clamped to BACKOFF_MAX_SECONDS

    @responses.activate
    def test_429_without_retry_after_falls_back_to_exponential(self, monkeypatch):
        """No Retry-After header → use _compute_backoff (base * 2**attempt)."""
        monkeypatch.setattr(logto, "BACKOFF_BASE_SECONDS", 2.0)
        monkeypatch.setattr(logto, "BACKOFF_MAX_SECONDS", 32.0)
        monkeypatch.setattr(logto, "BACKOFF_JITTER_FRACTION", 0.0)

        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={"error": "rate limited"},
            status=429,
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
        )

        sleeps: list[float] = []
        monkeypatch.setattr(logto.time, "sleep", lambda s: sleeps.append(s))

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        client.find_application("does-not-exist")

        assert len(sleeps) == 1
        # Formulaic: 2 * 2**0 = 2 (no jitter for determinism here).
        assert sleeps[0] == pytest.approx(2.0, abs=0.01)

    @responses.activate
    def test_429_honors_retry_after_http_date(self, monkeypatch):
        """Retry-After can also be an HTTP-date — parse via email.utils."""
        monkeypatch.setattr(logto, "BACKOFF_BASE_SECONDS", 2.0)
        monkeypatch.setattr(logto, "BACKOFF_MAX_SECONDS", 32.0)
        monkeypatch.setattr(logto, "BACKOFF_JITTER_FRACTION", 0.0)

        # Build an HTTP-date 7 seconds in the future.
        import email.utils as _eu
        future = time.time() + 7
        http_date = _eu.formatdate(future, usegmt=True)

        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={"error": "rate limited"},
            status=429,
            headers={"Retry-After": http_date},
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
        )

        sleeps: list[float] = []
        monkeypatch.setattr(logto.time, "sleep", lambda s: sleeps.append(s))

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        client.find_application("does-not-exist")

        assert len(sleeps) == 1
        # ~7s (small slop because time.time() ticks between header build
        # and parse, and we don't pin time in tests).
        assert sleeps[0] == pytest.approx(7.0, abs=1.5)


# ---------------------------------------------------------------------------
# Server-side search (R6)
# ---------------------------------------------------------------------------


class TestServerSideSearch:
    """list-applications must pass ?search=<name>, not scan client-side."""

    @responses.activate
    def test_find_application_uses_search_param(self):
        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[{"id": "spa-1", "name": "foo-frontend", "type": "SPA"}],
            status=200,
            match=[matchers.query_param_matcher({"search": "foo-frontend"})],
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.find_application("foo-frontend", app_type="SPA")
        assert result == {"id": "spa-1", "name": "foo-frontend", "type": "SPA"}

        # Verify the call shape
        list_call = next(c for c in responses.calls if "/api/applications" in c.request.url)
        assert "search=foo-frontend" in list_call.request.url

    @responses.activate
    def test_find_resource_uses_search_param(self):
        _stub_token_endpoint()
        indicator = "https://foo.example.test/api"
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/resources",
            json=[{"id": "res-1", "indicator": indicator, "name": "foo API"}],
            status=200,
            match=[matchers.query_param_matcher({"search": indicator})],
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.find_resource(indicator)
        assert result["id"] == "res-1"


# ---------------------------------------------------------------------------
# Management token caching (R6)
# ---------------------------------------------------------------------------


class TestMgmtTokenCache:
    """Token cached at ~/.cache/agentic-core/mgmt-token.json with mode 0600,
    60s pre-expiry refresh."""

    @responses.activate
    def test_token_cache_file_mode_0600(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "mgmt-token.json"
        monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)

        _stub_token_endpoint(access_token="abc", expires_in=3600)

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        client.get_management_token()

        assert cache_path.exists()
        mode = stat.S_IMODE(os.stat(cache_path).st_mode)
        assert mode == 0o600, f"Cache file mode is 0o{mode:o}, expected 0o600"

        data = json.loads(cache_path.read_text())
        assert data["access_token"] == "abc"
        assert "expires_at" in data
        assert "minted_at" in data

    @responses.activate
    def test_token_cache_used_when_fresh(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "mgmt-token.json"
        monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "access_token": "cached-token",
                    "expires_at": time.time() + 3600,
                    "minted_at": time.time(),
                }
            )
        )
        os.chmod(cache_path, 0o600)

        # No token endpoint stubbed — if the client tries to mint, it fails.
        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        token = client.get_management_token()
        assert token == "cached-token"
        # And nothing was minted.
        assert len(responses.calls) == 0

    @responses.activate
    def test_token_refreshed_when_within_60s_of_expiry(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "mgmt-token.json"
        monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Expires in 30s — under the 60s leeway, must refresh.
        cache_path.write_text(
            json.dumps(
                {
                    "access_token": "old-token",
                    "expires_at": time.time() + 30,
                    "minted_at": time.time() - 3570,
                }
            )
        )
        os.chmod(cache_path, 0o600)

        _stub_token_endpoint(access_token="new-token", expires_in=3600)

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        token = client.get_management_token()
        assert token == "new-token"


# ---------------------------------------------------------------------------
# Admin credential (bytearray) usage
# ---------------------------------------------------------------------------


class TestAdminCredential:
    """admin_credential is the M2M client_secret used to mint mgmt tokens."""

    @responses.activate
    def test_admin_credential_used_as_client_secret(self):
        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        client.find_application("anything")

        token_call = next(c for c in responses.calls if "/oidc/token" in c.request.url)
        body = token_call.request.body
        # httpx form encodes — body is bytes or str
        if isinstance(body, bytes):
            body = body.decode()
        assert ADMIN_M2M_ID in body
        assert ADMIN_M2M_SECRET in body
        assert "grant_type=client_credentials" in body
        encoded = MGMT_RESOURCE.replace(":", "%3A").replace("/", "%2F")
        assert encoded in body or MGMT_RESOURCE in body


# ---------------------------------------------------------------------------
# provision_app — per-app distinct credentials
# ---------------------------------------------------------------------------


def _stub_provision_responses(slug: str, *, spa_id: str, m2m_id: str, m2m_secret: str,
                              resource_id: str, user_id: str) -> None:
    """Stub all Logto endpoints needed for one provision_app() call.

    Every GET stub uses ``query_param_matcher`` so the integration
    flow would fail loudly if any find_* call site dropped the
    ``?search=<name>`` parameter from spec §R6.
    """
    indicator = f"https://{slug}.example.test/api"
    # Find lookups: all return empty (creating fresh).
    # SPA find: search=<slug>-frontend
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/applications",
        json=[],
        status=200,
        match=[matchers.query_param_matcher({"search": f"{slug}-frontend"})],
    )
    # M2M find: search=<slug>-agent
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/applications",
        json=[],
        status=200,
        match=[matchers.query_param_matcher({"search": f"{slug}-agent"})],
    )
    # Resource find: search=<indicator>
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/resources",
        json=[],
        status=200,
        match=[matchers.query_param_matcher({"search": indicator})],
    )
    # User find: search=app_<slug> (hyphens sanitized to underscores)
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/users",
        json=[],
        status=200,
        match=[matchers.query_param_matcher({"search": f"app_{slug.replace('-', '_')}"})],
    )
    # Create SPA app
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/applications",
        json={"id": spa_id, "name": f"{slug}-frontend", "type": "SPA"},
        status=200,
    )
    # Create M2M app
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/applications",
        json={
            "id": m2m_id,
            "name": f"{slug}-agent",
            "type": "MachineToMachine",
            "secret": m2m_secret,
        },
        status=200,
    )
    # Create API resource
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/resources",
        json={
            "id": resource_id,
            "indicator": f"https://{slug}.example.test/api",
            "name": f"{slug} API",
        },
        status=200,
    )
    # Create scopes (read + write)
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/resources/{resource_id}/scopes",
        json={"id": f"{resource_id}-read", "name": "read"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/resources/{resource_id}/scopes",
        json={"id": f"{resource_id}-write", "name": "write"},
        status=200,
    )
    # Create per-app user
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/users",
        json={"id": user_id, "username": f"app_{slug.replace('-', '_')}"},
        status=200,
    )
    # Roles. Scopes on the resource are inert until a role carries them and a
    # principal is assigned that role, so provisioning always follows
    # create_scopes with one User role + one MachineToMachine role.
    _stub_role_responses(slug)


def _stub_role_responses(
    slug: str, *, user_role_id: str = "role-user", agent_role_id: str = "role-agent"
) -> None:
    """Stub find_role (empty tenant), both role creates, and both assignments."""
    responses.add(responses.GET, f"{LOGTO_ENDPOINT}/api/roles", json=[], status=200)
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/roles",
        json={"id": user_role_id, "name": f"{slug}-user", "type": "User"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/roles",
        json={"id": agent_role_id, "name": f"{slug}-agent", "type": "MachineToMachine"},
        status=200,
    )
    for rid in (user_role_id, agent_role_id):
        responses.add(
            responses.GET, f"{LOGTO_ENDPOINT}/api/roles/{rid}/users", json=[], status=200
        )
        responses.add(
            responses.GET, f"{LOGTO_ENDPOINT}/api/roles/{rid}/applications", json=[], status=200
        )
        responses.add(
            responses.POST, f"{LOGTO_ENDPOINT}/api/roles/{rid}/users", json={}, status=201
        )
        responses.add(
            responses.POST, f"{LOGTO_ENDPOINT}/api/roles/{rid}/applications", json={}, status=201
        )


class TestProvisionApp:
    """Each call to provision_app() must produce fully distinct resources."""

    @responses.activate
    def test_two_consecutive_calls_produce_distinct_ids(self, tmp_path, monkeypatch):
        # Token cache must be shared across both calls so we don't re-mint per call.
        cache_path = tmp_path / "mgmt-token.json"
        monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)

        _stub_token_endpoint(access_token="t1", expires_in=3600)
        _stub_provision_responses(
            "foo",
            spa_id="spa-foo",
            m2m_id="m2m-foo",
            m2m_secret="secret-foo",
            resource_id="res-foo",
            user_id="user-foo",
        )
        _stub_provision_responses(
            "bar",
            spa_id="spa-bar",
            m2m_id="m2m-bar",
            m2m_secret="secret-bar",
            resource_id="res-bar",
            user_id="user-bar",
        )

        result_foo = logto.provision_app(
            slug="foo",
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result_bar = logto.provision_app(
            slug="bar",
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )

        # All four critical identifiers must differ.
        assert result_foo["spa_client_id"] != result_bar["spa_client_id"]
        assert result_foo["m2m_app_id"] != result_bar["m2m_app_id"]
        assert result_foo["m2m_app_secret"] != result_bar["m2m_app_secret"]
        assert result_foo["api_resource"] != result_bar["api_resource"]
        assert result_foo["user_sub"] != result_bar["user_sub"]

        # Sub follows the app_<slug> convention (hyphens -> underscores).
        assert result_foo["user_sub"] == "app_foo"
        assert result_bar["user_sub"] == "app_bar"

        # API resource follows the per-app URL pattern.
        assert result_foo["api_resource"] == "https://foo.example.test/api"
        assert result_bar["api_resource"] == "https://bar.example.test/api"

    @responses.activate
    def test_two_apps_post_distinct_application_bodies(self, tmp_path, monkeypatch):
        """R15: assert via responses.calls inspection that two POSTs to
        /api/applications carry distinct `name` payloads."""
        cache_path = tmp_path / "mgmt-token.json"
        monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)

        _stub_token_endpoint(access_token="t1", expires_in=3600)
        _stub_provision_responses(
            "foo",
            spa_id="spa-foo",
            m2m_id="m2m-foo",
            m2m_secret="secret-foo",
            resource_id="res-foo",
            user_id="user-foo",
        )
        _stub_provision_responses(
            "bar",
            spa_id="spa-bar",
            m2m_id="m2m-bar",
            m2m_secret="secret-bar",
            resource_id="res-bar",
            user_id="user-bar",
        )

        logto.provision_app(
            slug="foo",
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        logto.provision_app(
            slug="bar",
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )

        # Collect POST bodies to /api/applications across both calls.
        app_post_bodies: list[dict] = []
        for call in responses.calls:
            url = call.request.url
            if "/api/applications" in url and call.request.method == "POST":
                body = call.request.body
                if isinstance(body, bytes):
                    body = body.decode()
                app_post_bodies.append(json.loads(body))

        # 2 apps × (SPA + M2M) = 4 POSTs total
        assert len(app_post_bodies) == 4
        names = [b["name"] for b in app_post_bodies]
        assert "foo-frontend" in names
        assert "foo-agent" in names
        assert "bar-frontend" in names
        assert "bar-agent" in names
        # All four names are distinct.
        assert len(set(names)) == 4

    @responses.activate
    def test_provision_app_returns_expected_envelope_shape(self, tmp_path, monkeypatch):
        """Returned dict must contain the keys the orchestrator + JSON schema need."""
        cache_path = tmp_path / "mgmt-token.json"
        monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)

        _stub_token_endpoint()
        _stub_provision_responses(
            "myapp",
            spa_id="spa-myapp",
            m2m_id="m2m-myapp",
            m2m_secret="secret-myapp",
            resource_id="res-myapp",
            user_id="user-myapp",
        )

        result = logto.provision_app(
            slug="myapp",
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )

        for key in (
            "spa_client_id",
            "m2m_app_id",
            "m2m_app_secret",
            "api_resource",
            "user_sub",
            "endpoint",
            "scopes",
        ):
            assert key in result, f"missing key {key} in provision_app result"

        assert result["endpoint"] == LOGTO_ENDPOINT
        assert sorted(result["scopes"]) == ["read", "write"]


# ---------------------------------------------------------------------------
# Concurrent-provision safety: 409 on create POST → re-fetch via find_*
# ---------------------------------------------------------------------------


class TestConcurrent409Handling:
    """Two parallel provision_app calls for the same slug must NOT crash.

    The first POST creates the resource; the second POST races, gets a
    409 (or 422 — Logto can return either depending on uniqueness
    constraint), and must transparently fall back to a find_* call to
    return the existing record.
    """

    @responses.activate
    def test_create_spa_application_409_falls_back_to_find(self):
        _stub_token_endpoint()
        # First find: empty (so we proceed to POST).
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": "raced-frontend"})],
        )
        # POST: 409 (concurrent provisioner won the race).
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={"error": "name already taken"},
            status=409,
        )
        # Fallback find: returns the SPA the other provisioner created.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[{"id": "spa-raced", "name": "raced-frontend", "type": "SPA"}],
            status=200,
            match=[matchers.query_param_matcher({"search": "raced-frontend"})],
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.create_spa_application("raced")
        assert result == {"id": "spa-raced", "name": "raced-frontend", "type": "SPA"}

    @responses.activate
    def test_create_m2m_application_409_falls_back_to_find(self):
        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": "raced-agent"})],
        )
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={"error": "name already taken"},
            status=409,
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[{
                "id": "m2m-raced",
                "name": "raced-agent",
                "type": "MachineToMachine",
                "secret": "winner-secret",
            }],
            status=200,
            match=[matchers.query_param_matcher({"search": "raced-agent"})],
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.create_m2m_application("raced")
        assert result["id"] == "m2m-raced"
        assert result["secret"] == "winner-secret"

    @responses.activate
    def test_create_api_resource_409_falls_back_to_find(self):
        _stub_token_endpoint()
        indicator = "https://raced.example.test/api"
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/resources",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": indicator})],
        )
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/resources",
            json={"error": "indicator already exists"},
            status=409,
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/resources",
            json=[{"id": "res-raced", "indicator": indicator, "name": "raced API"}],
            status=200,
            match=[matchers.query_param_matcher({"search": indicator})],
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.create_api_resource("raced", indicator)
        assert result["id"] == "res-raced"

    @responses.activate
    def test_create_user_409_falls_back_to_find(self):
        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/users",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": "app_raced"})],
        )
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/users",
            json={"error": "username already taken"},
            status=422,
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/users",
            json=[{"id": "user-raced", "username": "app_raced"}],
            status=200,
            match=[matchers.query_param_matcher({"search": "app_raced"})],
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.create_user("raced")
        assert result["id"] == "user-raced"

    @responses.activate
    def test_create_user_sanitizes_hyphenated_slug_to_underscore_username(self):
        # Regression (live-surfaced): a hyphenated slug like
        # ``my-sample-notes-app`` produced username ``app-my-sample-notes-app``,
        # which Logto rejects (username must match ^[A-Za-z_]\w*$). Hyphens ->
        # underscores yields a valid ``app_my_sample_notes_app``.
        _stub_token_endpoint()
        expected_username = "app_my_sample_notes_app"
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/users",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": expected_username})],
        )
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/users",
            json={"id": "user-vdd", "username": expected_username},
            status=200,
            match=[matchers.json_params_matcher(
                {"username": expected_username, "name": "App my-sample-notes-app"}
            )],
        )
        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.create_user("my-sample-notes-app")
        assert result["username"] == expected_username
        assert "-" not in result["username"]


# ---------------------------------------------------------------------------
# P1: M2M adopt path must never yield an empty secret silently.
#
# On resume where STEP_LOGTO wasn't checkpointed, create_m2m_application takes
# the adopt path (find_application returns the existing M2M app). Logto list
# endpoints often OMIT the secret field, so a naive adopt could return an empty
# secret that cascades into an empty GitHub/Railway secret with NO failure. The
# fix: detail-fetch the secret, else raise LogtoError (fail loud).
# ---------------------------------------------------------------------------


class TestM2MAdoptSecretRecovery:
    @responses.activate
    def test_adopt_m2m_missing_secret_recovers_from_detail_get(self):
        """Adopted M2M app lacks a secret in the LIST result → detail GET
        supplies it → adoption succeeds with the recovered secret."""
        _stub_token_endpoint()
        # find (list ?search=) returns the M2M app WITHOUT a secret field.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[{"id": "m2m-x", "name": "app-agent", "type": "MachineToMachine"}],
            status=200,
            match=[matchers.query_param_matcher({"search": "app-agent"})],
        )
        # detail GET on the id carries the secret.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/m2m-x",
            json={
                "id": "m2m-x",
                "name": "app-agent",
                "type": "MachineToMachine",
                "secret": "recovered-secret-value",
            },
            status=200,
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.create_m2m_application("app")
        assert result["id"] == "m2m-x"
        assert result["secret"] == "recovered-secret-value"

    @responses.activate
    def test_adopt_m2m_missing_secret_everywhere_raises(self):
        """Neither the LIST result nor the detail GET carries a secret →
        raise LogtoError rather than silently return an empty secret."""
        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[{"id": "m2m-x", "name": "app-agent", "type": "MachineToMachine"}],
            status=200,
            match=[matchers.query_param_matcher({"search": "app-agent"})],
        )
        # detail GET ALSO lacks the secret (still unrecoverable).
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/m2m-x",
            json={"id": "m2m-x", "name": "app-agent", "type": "MachineToMachine"},
            status=200,
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        with pytest.raises(logto.LogtoError):
            client.create_m2m_application("app")

    @responses.activate
    def test_adopt_m2m_missing_secret_everywhere_raises_distinct_type(self):
        """Fix #15(b): the unrecoverable-secret case raises a DISTINCT
        (non-retryable) type, not a plain LogtoError the orchestrator would
        retry forever on --resume."""
        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[{"id": "m2m-x", "name": "app-agent", "type": "MachineToMachine"}],
            status=200,
            match=[matchers.query_param_matcher({"search": "app-agent"})],
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/m2m-x",
            json={"id": "m2m-x", "name": "app-agent", "type": "MachineToMachine"},
            status=200,
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        with pytest.raises(logto.LogtoSecretUnrecoverable):
            client.create_m2m_application("app")

    @responses.activate
    def test_create_response_without_secret_raises_not_silent_empty(self):
        """Fix #15(a): the NORMAL create-POST path must also route through the
        secret guard — a create response lacking a usable secret fails loud
        (distinct type), never returns a silent empty credential."""
        _stub_token_endpoint()
        # find returns empty → proceed to POST (create path).
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": "app-agent"})],
        )
        # create POST response has NO secret field.
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={"id": "m2m-created", "name": "app-agent", "type": "MachineToMachine"},
            status=200,
        )
        # detail GET fallback ALSO lacks a secret → unrecoverable.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/m2m-created",
            json={"id": "m2m-created", "name": "app-agent", "type": "MachineToMachine"},
            status=200,
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        with pytest.raises(logto.LogtoSecretUnrecoverable):
            client.create_m2m_application("app")

    @responses.activate
    def test_create_response_with_secret_happy_path_unchanged(self):
        """Fix #15 regression guard: a create response that DOES carry a secret
        returns it as-is (no detail GET, no raise)."""
        _stub_token_endpoint()
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": "app-agent"})],
        )
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={
                "id": "m2m-created",
                "name": "app-agent",
                "type": "MachineToMachine",
                "secret": "fresh-secret",
            },
            status=200,
        )

        client = logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        result = client.create_m2m_application("app")
        assert result["secret"] == "fresh-secret"


# ---------------------------------------------------------------------------
# U4: incremental resource-ID persistence via the persist callback
# ---------------------------------------------------------------------------


class TestPersistCallback:
    """provision_app persists each resource ID the moment it is created.

    R2 / SC2: a mid-sequence failure must leave the already-created
    resources recorded in state (via the callback) so ``--resume`` can adopt
    them instead of orphaning them. The M2M secret is NEVER handed to the
    callback (state entropy gate forbids secrets).
    """

    @responses.activate
    def test_persist_invoked_incrementally_before_midsequence_failure(
        self, tmp_path, monkeypatch
    ):
        """API-resource creation 500s → SPA + M2M IDs persisted BEFORE the
        raise; api_resource + user_sub are NOT persisted (never reached)."""
        cache_path = tmp_path / "mgmt-token.json"
        monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)
        _stub_token_endpoint()

        slug = "midfail"
        indicator = f"https://{slug}.example.test/api"
        # SPA: find empty → create OK.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": f"{slug}-frontend"})],
        )
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={"id": "spa-mid", "name": f"{slug}-frontend", "type": "SPA"},
            status=200,
        )
        # M2M: find empty → create OK (carries a secret we must NOT persist).
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": f"{slug}-agent"})],
        )
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/applications",
            json={
                "id": "m2m-mid",
                "name": f"{slug}-agent",
                "type": "MachineToMachine",
                "secret": "SUPERSECRETm2mVALUE0000",
            },
            status=200,
        )
        # API resource: find empty → create 500 (mid-sequence failure).
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/resources",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": indicator})],
        )
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/resources",
            json={"error": "boom"},
            status=500,
        )

        persisted: list[tuple[str, str]] = []

        with pytest.raises(logto.LogtoError):
            logto.provision_app(
                slug=slug,
                platform_domain="example.test",
                endpoint=LOGTO_ENDPOINT,
                admin_app_id=ADMIN_M2M_ID,
                admin_credential=_admin_cred(),
                persist=lambda k, v: persisted.append((k, v)),
            )

        keys = [k for k, _ in persisted]
        # SPA + M2M IDs recorded before the API-resource failure.
        assert "logto_spa_client_id" in keys
        assert "logto_m2m_app_id" in keys
        # Not reached — never persisted.
        assert "logto_api_resource" not in keys
        assert "logto_user_sub" not in keys

        as_dict = dict(persisted)
        assert as_dict["logto_spa_client_id"] == "spa-mid"
        assert as_dict["logto_m2m_app_id"] == "m2m-mid"

        # Entropy-gate guard: the M2M secret never flows to the callback.
        assert all("SUPERSECRETm2mVALUE0000" != v for _, v in persisted)
        assert all("secret" not in k for k, _ in persisted)

    @responses.activate
    def test_resume_all_resources_exist_adopts_via_finders_no_creates(
        self, tmp_path, monkeypatch
    ):
        """R1: on resume with every Logto resource already present, each
        create_* adopts via its finder — zero create POSTs — and the adopted
        IDs are (re-)persisted through the callback."""
        cache_path = tmp_path / "mgmt-token.json"
        monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)
        _stub_token_endpoint()

        slug = "resumed"
        indicator = f"https://{slug}.example.test/api"
        # SPA find → existing.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[{"id": "spa-existing", "name": f"{slug}-frontend", "type": "SPA"}],
            status=200,
            match=[matchers.query_param_matcher({"search": f"{slug}-frontend"})],
        )
        # M2M find → existing (secret present but must not be persisted).
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[
                {
                    "id": "m2m-existing",
                    "name": f"{slug}-agent",
                    "type": "MachineToMachine",
                    "secret": "EXISTINGm2mSECRETvalue00",
                }
            ],
            status=200,
            match=[matchers.query_param_matcher({"search": f"{slug}-agent"})],
        )
        # Resource find → existing.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/resources",
            json=[{"id": "res-existing", "indicator": indicator, "name": f"{slug} API"}],
            status=200,
            match=[matchers.query_param_matcher({"search": indicator})],
        )
        # Scopes already exist: POST 409 → GET list resolves read/write.
        for _ in range(2):
            responses.add(
                responses.POST,
                f"{LOGTO_ENDPOINT}/api/resources/res-existing/scopes",
                json={"error": "exists"},
                status=409,
            )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/resources/res-existing/scopes",
            json=[{"id": "s-r", "name": "read"}, {"id": "s-w", "name": "write"}],
            status=200,
        )
        # User find → existing.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/users",
            json=[{"id": "user-existing", "username": f"app_{slug.replace('-', '_')}"}],
            status=200,
            match=[matchers.query_param_matcher({"search": f"app_{slug.replace('-', '_')}"})],
        )
        # Roles already exist too, with their scopes attached and their
        # principals assigned — so resume adopts them and issues no POST.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/roles",
            json=[
                {"id": "role-user-existing", "name": f"{slug}-user", "type": "User"},
                {
                    "id": "role-agent-existing",
                    "name": f"{slug}-agent",
                    "type": "MachineToMachine",
                },
            ],
            status=200,
        )
        for rid in ("role-user-existing", "role-agent-existing"):
            responses.add(
                responses.GET,
                f"{LOGTO_ENDPOINT}/api/roles/{rid}/scopes",
                json=[{"id": "s-r", "name": "read"}, {"id": "s-w", "name": "write"}],
                status=200,
            )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/roles/role-user-existing/users",
            json=[{"id": "user-existing"}],
            status=200,
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/roles/role-agent-existing/applications",
            json=[{"id": "m2m-existing"}],
            status=200,
        )

        persisted: dict[str, str] = {}
        result = logto.provision_app(
            slug=slug,
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
            persist=lambda k, v: persisted.__setitem__(k, v),
        )

        # No create POST to applications / resources / users (adopted via finder).
        create_posts = [
            c
            for c in responses.calls
            if c.request.method == "POST"
            and c.request.url.split("?")[0].rstrip("/").endswith(
                ("/api/applications", "/api/resources", "/api/users", "/api/roles")
            )
        ]
        assert create_posts == [], f"unexpected create POSTs: {create_posts}"

        # Adopted IDs persisted through the callback.
        assert persisted["logto_spa_client_id"] == "spa-existing"
        assert persisted["logto_m2m_app_id"] == "m2m-existing"
        assert persisted["logto_api_resource"] == indicator
        assert persisted["logto_user_sub"] == f"app_{slug.replace('-', '_')}"

        # Secret never persisted, but still returned to the caller in-memory.
        assert all(v != "EXISTINGm2mSECRETvalue00" for v in persisted.values())
        assert result["m2m_app_secret"] == "EXISTINGm2mSECRETvalue00"

    @responses.activate
    def test_persist_default_none_is_noop(self, tmp_path, monkeypatch):
        """Back-compat: omitting ``persist`` leaves provision_app unchanged."""
        cache_path = tmp_path / "mgmt-token.json"
        monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)
        _stub_token_endpoint()
        _stub_provision_responses(
            "noop",
            spa_id="spa-noop",
            m2m_id="m2m-noop",
            m2m_secret="secret-noop",
            resource_id="res-noop",
            user_id="user-noop",
        )
        result = logto.provision_app(
            slug="noop",
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )
        assert result["spa_client_id"] == "spa-noop"


# ---------------------------------------------------------------------------
# U6: SPA redirect-URI merge PATCH (prod sign-in wiring, SC3)
#
# CRITICAL merge semantics (Risk R-d): the prod redirect URI is UNIONed into
# the SPA app's existing oidcClientMetadata.redirectUris — never a replace —
# so localhost dev URIs survive. Field name is oidcClientMetadata.redirectUris,
# confirmed against create_spa_application which sets exactly that path.
# ---------------------------------------------------------------------------


class TestUpdateRedirectUris:
    def _client(self) -> logto.LogtoManagementClient:
        return logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )

    @responses.activate
    def test_merge_preserves_localhost_and_adds_prod(self):
        """SC3 (critical): given ["http://localhost:5173/callback"], the PATCH
        body carries BOTH localhost AND https://<domain>/callback."""
        _stub_token_endpoint()
        app_id = "spa-1"
        prod = "https://foo-production.up.railway.app/callback"
        # GET current app state — localhost only.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "type": "SPA",
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback"],
                    "postLogoutRedirectUris": ["http://localhost:5173"],
                },
            },
            status=200,
        )
        # PATCH echoes back the merged state (server truth).
        responses.add(
            responses.PATCH,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback", prod],
                    "postLogoutRedirectUris": ["http://localhost:5173"],
                },
            },
            status=200,
        )
        # Fix #16: follow-up authoritative GET confirming the prod URI persisted.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback", prod],
                    "postLogoutRedirectUris": ["http://localhost:5173"],
                },
            },
            status=200,
        )

        result = self._client().update_application_redirect_uris(app_id, [prod])

        # The PATCH request body must contain BOTH URIs (merge, not replace).
        patch_call = next(
            c for c in responses.calls if c.request.method == "PATCH"
        )
        body = json.loads(patch_call.request.body)
        sent = body["oidcClientMetadata"]["redirectUris"]
        assert "http://localhost:5173/callback" in sent
        assert prod in sent
        # postLogoutRedirectUris is preserved (not clobbered).
        assert body["oidcClientMetadata"]["postLogoutRedirectUris"] == [
            "http://localhost:5173"
        ]
        # Return reflects the post-state including the prod URI.
        assert prod in result["redirect_uris"]
        assert result["patched"] is True

    @responses.activate
    def test_idempotent_when_prod_uri_already_present_no_patch(self):
        """Resume/idempotency: prod URI already present → no PATCH issued."""
        _stub_token_endpoint()
        app_id = "spa-1"
        prod = "https://foo-production.up.railway.app/callback"
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback", prod],
                },
            },
            status=200,
        )

        result = self._client().update_application_redirect_uris(app_id, [prod])

        assert result["patched"] is False
        assert prod in result["redirect_uris"]
        # No PATCH was sent — only the GET happened (plus the token mint).
        assert not any(c.request.method == "PATCH" for c in responses.calls)

    @responses.activate
    def test_empty_existing_uris_adds_prod(self):
        """An SPA with no redirectUris yet still gets the prod URI added."""
        _stub_token_endpoint()
        app_id = "spa-1"
        prod = "https://foo-production.up.railway.app/callback"
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={"id": app_id, "oidcClientMetadata": {}},
            status=200,
        )
        responses.add(
            responses.PATCH,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={"id": app_id, "oidcClientMetadata": {"redirectUris": [prod]}},
            status=200,
        )
        # Fix #16: follow-up authoritative GET confirming persistence.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={"id": app_id, "oidcClientMetadata": {"redirectUris": [prod]}},
            status=200,
        )

        result = self._client().update_application_redirect_uris(app_id, [prod])
        assert result["patched"] is True
        assert prod in result["redirect_uris"]

    @responses.activate
    def test_patch_2xx_but_prod_uri_not_persisted_raises(self):
        """Fix #16 (critical): PATCH returns 2xx but the authoritative follow-up
        GET shows the prod URI did NOT persist → raise, rather than falsely
        passing by echoing the requested list."""
        _stub_token_endpoint()
        app_id = "spa-1"
        prod = "https://foo-production.up.railway.app/callback"
        # Initial GET — localhost only.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback"],
                },
            },
            status=200,
        )
        # PATCH returns 2xx but with redirectUris that OMIT the prod URI.
        responses.add(
            responses.PATCH,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback"],
                },
            },
            status=200,
        )
        # Authoritative follow-up GET ALSO lacks the prod URI (never persisted).
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback"],
                },
            },
            status=200,
        )

        with pytest.raises(logto.LogtoError):
            self._client().update_application_redirect_uris(app_id, [prod])

    @responses.activate
    def test_patch_2xx_empty_redirect_uris_raises(self):
        """Fix #16: a PATCH that reports empty redirectUris and a follow-up GET
        showing none persisted must raise (not fall back to the requested list)."""
        _stub_token_endpoint()
        app_id = "spa-1"
        prod = "https://foo-production.up.railway.app/callback"
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={"id": app_id, "oidcClientMetadata": {}},
            status=200,
        )
        responses.add(
            responses.PATCH,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={"id": app_id, "oidcClientMetadata": {}},
            status=200,
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={"id": app_id, "oidcClientMetadata": {}},
            status=200,
        )

        with pytest.raises(logto.LogtoError):
            self._client().update_application_redirect_uris(app_id, [prod])

    @responses.activate
    def test_module_wrapper_constructs_client_and_merges(self):
        """The module-level wrapper the orchestrator calls threads through to
        the client method."""
        _stub_token_endpoint()
        app_id = "spa-1"
        prod = "https://foo-production.up.railway.app/callback"
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback"]
                },
            },
            status=200,
        )
        responses.add(
            responses.PATCH,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback", prod]
                },
            },
            status=200,
        )
        # Fix #16: follow-up authoritative GET confirming persistence.
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={
                "id": app_id,
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback", prod]
                },
            },
            status=200,
        )

        result = logto.update_app_redirect_uris(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            spa_app_id=app_id,
            prod_redirect_uri=prod,
            admin_credential=_admin_cred(),
        )
        assert prod in result["redirect_uris"]


# ---------------------------------------------------------------------------
# Delete methods (U8 / teardown) — DELETE plumbing for per-app Logto
# resources. Method + path assertions via `responses`. A 404 on delete is
# tolerated (already-absent) so a partial teardown never crashes.
# ---------------------------------------------------------------------------


class TestDeleteMethods:
    def _client(self) -> logto.LogtoManagementClient:
        return logto.LogtoManagementClient(
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )

    @responses.activate
    def test_delete_application_issues_delete_on_app_path(self):
        _stub_token_endpoint()
        app_id = "spa-123"
        responses.add(
            responses.DELETE,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            status=204,
        )
        deleted = self._client().delete_application(app_id)
        assert deleted is True
        # last non-token call is the DELETE against the app path
        req = responses.calls[-1].request
        assert req.method == "DELETE"
        assert req.url == f"{LOGTO_ENDPOINT}/api/applications/{app_id}"

    @responses.activate
    def test_delete_application_404_is_tolerated_as_already_absent(self):
        _stub_token_endpoint()
        app_id = "gone-1"
        responses.add(
            responses.DELETE,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            status=404,
        )
        deleted = self._client().delete_application(app_id)
        assert deleted is False

    @responses.activate
    def test_delete_api_resource_issues_delete_on_resource_path(self):
        _stub_token_endpoint()
        rid = "res-9"
        responses.add(
            responses.DELETE,
            f"{LOGTO_ENDPOINT}/api/resources/{rid}",
            status=204,
        )
        deleted = self._client().delete_api_resource(rid)
        assert deleted is True
        req = responses.calls[-1].request
        assert req.method == "DELETE"
        assert req.url == f"{LOGTO_ENDPOINT}/api/resources/{rid}"

    @responses.activate
    def test_delete_user_issues_delete_on_user_path(self):
        _stub_token_endpoint()
        uid = "user-7"
        responses.add(
            responses.DELETE,
            f"{LOGTO_ENDPOINT}/api/users/{uid}",
            status=204,
        )
        deleted = self._client().delete_user(uid)
        assert deleted is True
        req = responses.calls[-1].request
        assert req.method == "DELETE"
        assert req.url == f"{LOGTO_ENDPOINT}/api/users/{uid}"

    @responses.activate
    def test_delete_500_raises_logto_error(self):
        _stub_token_endpoint()
        app_id = "spa-err"
        responses.add(
            responses.DELETE,
            f"{LOGTO_ENDPOINT}/api/applications/{app_id}",
            json={"error": "boom"},
            status=500,
        )
        with pytest.raises(logto.LogtoError):
            self._client().delete_application(app_id)
