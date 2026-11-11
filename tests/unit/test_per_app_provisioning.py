"""Unit tests for the R10 variant×invariant provisioning matrix.

``logto.provision_app`` accepts four booleans — ``with_spa``, ``with_m2m``,
``with_api_resource``, ``with_user`` — that let the orchestrator skip
resources irrelevant to the chosen variant. The mapping itself (variant →
which flags) lives in the orchestrator (task #8); here we just prove the
kwargs gate the right Management API calls.

Variant matrix from spec §R10 these tests cover:
  - ``full``: all four True (default)
  - ``lightweight-api``: SPA=False, others True (no UI to redirect to)
  - ``static-dashboard --with-auth``: SPA+User=True, M2M+API resource=False
  - ``static-dashboard --no-auth``: orchestrator skips provision_app entirely
    (not tested here — that's the orchestrator's call)

Strategy: stub Logto endpoints with ``responses``; assert on the count of
matching POSTs across the call set. Skipped resources MUST NOT POST.
"""

from __future__ import annotations


import pytest
import responses
from responses import matchers

from agentic_core.bootstrap import logto


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOGTO_ENDPOINT = "https://logto.test.example.com"
ADMIN_M2M_ID = "admin-m2m-id"
ADMIN_M2M_SECRET = "admin-m2m-secret"


def _admin_cred() -> bytearray:
    return bytearray(ADMIN_M2M_SECRET, "utf-8")


def _stub_token_endpoint() -> None:
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/oidc/token",
        json={"access_token": "mgmt-token", "expires_in": 3600, "token_type": "Bearer"},
        status=200,
    )


def _stub_find_application_empty(name: str) -> None:
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/applications",
        json=[],
        status=200,
        match=[matchers.query_param_matcher({"search": name})],
    )


def _stub_find_resource_empty(indicator: str) -> None:
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/resources",
        json=[],
        status=200,
        match=[matchers.query_param_matcher({"search": indicator})],
    )


def _stub_find_user_empty(username: str) -> None:
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/users",
        json=[],
        status=200,
        match=[matchers.query_param_matcher({"search": username})],
    )


def _stub_create_spa(slug: str, spa_id: str = "spa-id") -> None:
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/applications",
        json={"id": spa_id, "name": f"{slug}-frontend", "type": "SPA"},
        status=200,
    )


def _stub_create_m2m(slug: str, m2m_id: str = "m2m-id", secret: str = "m2m-sec") -> None:
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/applications",
        json={
            "id": m2m_id,
            "name": f"{slug}-agent",
            "type": "MachineToMachine",
            "secret": secret,
        },
        status=200,
    )


def _stub_create_resource(slug: str, indicator: str, resource_id: str = "res-id") -> None:
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/resources",
        json={"id": resource_id, "indicator": indicator, "name": f"{slug} API"},
        status=200,
    )
    # Scopes — only created when api resource is.
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


def _stub_create_user(slug: str, user_id: str = "user-id") -> None:
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/users",
        json={"id": user_id, "username": f"app_{slug.replace('-', '_')}"},
        status=200,
    )


def _stub_roles(user_role_id: str = "role-user", agent_role_id: str = "role-agent") -> None:
    """Stub the role endpoints provision_app hits after scopes exist.

    Scopes created on a resource are inert until a role carries them and a
    principal is assigned that role, so provisioning always follows
    create_scopes with role creation + assignment.
    """
    # find_role -> not found (list is empty), then GET again for scope check.
    responses.add(responses.GET, f"{LOGTO_ENDPOINT}/api/roles", json=[], status=200)
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/roles",
        json={"id": user_role_id, "name": "role", "type": "User"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/roles",
        json={"id": agent_role_id, "name": "role", "type": "MachineToMachine"},
        status=200,
    )
    for rid in (user_role_id, agent_role_id):
        responses.add(responses.GET, f"{LOGTO_ENDPOINT}/api/roles/{rid}/users", json=[], status=200)
        responses.add(
            responses.GET, f"{LOGTO_ENDPOINT}/api/roles/{rid}/applications", json=[], status=200
        )
        responses.add(
            responses.POST, f"{LOGTO_ENDPOINT}/api/roles/{rid}/users", json={}, status=201
        )
        responses.add(
            responses.POST, f"{LOGTO_ENDPOINT}/api/roles/{rid}/applications", json={}, status=201
        )


def _count_posts_to(path_suffix: str) -> int:
    """Count POSTs that target a URL ending with ``path_suffix``."""
    n = 0
    for c in responses.calls:
        if c.request.method != "POST":
            continue
        url = c.request.url
        # Strip query string for matching.
        url_no_q = url.split("?", 1)[0]
        if url_no_q.endswith(path_suffix):
            n += 1
    return n


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "mgmt-token.json"
    monkeypatch.setattr(logto, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)
    monkeypatch.setattr(logto, "BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(logto, "BACKOFF_MAX_SECONDS", 0.0)
    monkeypatch.setattr(logto, "BACKOFF_JITTER_FRACTION", 0.0)
    yield


# ---------------------------------------------------------------------------
# Default behavior: all four True (full variant, backwards compatible)
# ---------------------------------------------------------------------------


class TestDefaultProvisioning:
    """All four resource flags default to True — preserves task #4 behavior."""

    @responses.activate
    def test_default_provisions_all_four_resources(self):
        slug = "myapp"
        indicator = f"https://{slug}.example.test/api"
        _stub_token_endpoint()
        _stub_find_application_empty(f"{slug}-frontend")
        _stub_find_application_empty(f"{slug}-agent")
        _stub_find_resource_empty(indicator)
        # Logto usernames must match ^[A-Za-z_]\\w*$, so create_user
        # sanitizes hyphens to underscores — the search term does too.
        _stub_find_user_empty(f"app_{slug.replace('-', '_')}")
        _stub_create_spa(slug)
        _stub_create_m2m(slug)
        _stub_roles()
        _stub_create_resource(slug, indicator)
        _stub_create_user(slug)

        result = logto.provision_app(
            slug=slug,
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
        )

        # Two POSTs to /api/applications (SPA + M2M), one to /api/resources,
        # two scope POSTs, one user POST.
        assert _count_posts_to("/api/applications") == 2
        assert _count_posts_to("/api/resources") == 1
        assert _count_posts_to("/api/users") == 1

        # Result has all keys populated.
        assert result["spa_client_id"]
        assert result["m2m_app_id"]
        assert result["m2m_app_secret"]
        assert result["api_resource"] == indicator
        assert result["user_sub"] == f"app_{slug.replace('-', '_')}"


# ---------------------------------------------------------------------------
# lightweight-api: SPA skipped, others True
# ---------------------------------------------------------------------------


class TestLightweightApiVariant:
    """``with_spa=False`` skips the SPA application POST entirely."""

    @responses.activate
    def test_with_spa_false_skips_spa_post(self):
        slug = "myapi"
        indicator = f"https://{slug}.example.test/api"
        _stub_token_endpoint()
        # NOTE: no SPA find stub registered — if the client tries it, responses
        # would raise ConnectionError. That's the strong assertion.
        _stub_find_application_empty(f"{slug}-agent")
        _stub_find_resource_empty(indicator)
        # Logto usernames must match ^[A-Za-z_]\\w*$, so create_user
        # sanitizes hyphens to underscores — the search term does too.
        _stub_find_user_empty(f"app_{slug.replace('-', '_')}")
        _stub_create_m2m(slug)
        _stub_roles()
        _stub_create_resource(slug, indicator)
        _stub_create_user(slug)

        result = logto.provision_app(
            slug=slug,
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
            with_spa=False,
        )

        # Only one POST to /api/applications: the M2M. No SPA.
        assert _count_posts_to("/api/applications") == 1
        assert _count_posts_to("/api/resources") == 1
        assert _count_posts_to("/api/users") == 1

        # Skipped resource is absent (or None).
        assert result.get("spa_client_id") in (None, "")
        # Present resources still populated.
        assert result["m2m_app_id"]
        assert result["api_resource"] == indicator
        assert result["user_sub"] == f"app_{slug.replace('-', '_')}"


# ---------------------------------------------------------------------------
# static-dashboard --with-auth: SPA+User, no M2M, no API resource
# ---------------------------------------------------------------------------


class TestStaticDashboardWithAuth:
    """``with_m2m=False, with_api_resource=False`` skips both M2M and resource."""

    @responses.activate
    def test_with_m2m_and_api_resource_false_skips_both(self):
        slug = "dash"
        _stub_token_endpoint()
        _stub_find_application_empty(f"{slug}-frontend")
        # Logto usernames must match ^[A-Za-z_]\\w*$, so create_user
        # sanitizes hyphens to underscores — the search term does too.
        _stub_find_user_empty(f"app_{slug.replace('-', '_')}")
        _stub_create_spa(slug)
        _stub_create_user(slug)
        # NO m2m, resource, or scope stubs — any attempted POST would 404 or
        # raise ConnectionError on the stub.

        result = logto.provision_app(
            slug=slug,
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
            with_m2m=False,
            with_api_resource=False,
        )

        # Only the SPA POST hit /api/applications.
        assert _count_posts_to("/api/applications") == 1
        # Zero POSTs to /api/resources.
        assert _count_posts_to("/api/resources") == 0
        # One user POST (for SPA login).
        assert _count_posts_to("/api/users") == 1

        # Skipped fields absent / falsy.
        assert result.get("m2m_app_id") in (None, "")
        assert result.get("m2m_app_secret") in (None, "")
        assert result.get("api_resource") in (None, "")
        # Present fields populated.
        assert result["spa_client_id"]
        assert result["user_sub"] == f"app_{slug.replace('-', '_')}"


# ---------------------------------------------------------------------------
# with_user=False: no user POST
# ---------------------------------------------------------------------------


class TestWithUserFalse:
    """``with_user=False`` skips the user creation POST."""

    @responses.activate
    def test_with_user_false_skips_user_post(self):
        slug = "headless"
        indicator = f"https://{slug}.example.test/api"
        _stub_token_endpoint()
        _stub_find_application_empty(f"{slug}-frontend")
        _stub_find_application_empty(f"{slug}-agent")
        _stub_find_resource_empty(indicator)
        _stub_create_spa(slug)
        _stub_create_m2m(slug)
        _stub_roles()
        _stub_create_resource(slug, indicator)
        # NO user stubs.

        result = logto.provision_app(
            slug=slug,
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
            with_user=False,
        )

        assert _count_posts_to("/api/users") == 0
        assert result.get("user_sub") in (None, "")
        # Other resources still present.
        assert result["spa_client_id"]
        assert result["m2m_app_id"]
        assert result["api_resource"] == indicator


# ---------------------------------------------------------------------------
# All-False degenerate case (orchestrator wouldn't actually call this, but
# the function should not crash and should produce a coherent empty envelope)
# ---------------------------------------------------------------------------


class TestAllFalse:
    """Edge case: all flags False — no calls beyond the token mint."""

    @responses.activate
    def test_all_false_makes_no_resource_calls(self):
        _stub_token_endpoint()

        result = logto.provision_app(
            slug="nothing",
            platform_domain="example.test",
            endpoint=LOGTO_ENDPOINT,
            admin_app_id=ADMIN_M2M_ID,
            admin_credential=_admin_cred(),
            with_spa=False,
            with_m2m=False,
            with_api_resource=False,
            with_user=False,
        )

        # No resource POSTs at all.
        assert _count_posts_to("/api/applications") == 0
        assert _count_posts_to("/api/resources") == 0
        assert _count_posts_to("/api/users") == 0
        # Result still has the shape, all fields falsy.
        assert result.get("spa_client_id") in (None, "")
        assert result.get("m2m_app_id") in (None, "")
        assert result.get("api_resource") in (None, "")
        assert result.get("user_sub") in (None, "")
        # Endpoint is always echoed.
        assert result["endpoint"] == LOGTO_ENDPOINT
