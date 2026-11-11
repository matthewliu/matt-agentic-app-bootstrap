"""Integration test — two consecutive ``orchestrate_create_app`` runs
produce fully distinct Logto credentials (task #17, spec R15).

This sits one level above ``tests/unit/test_bootstrap_logto.py``: that
unit suite proves ``provision_app`` per slug. Here we drive the full
orchestrator twice (foo, then bar) with the same in-memory keystore
credential and a shared management-token cache, and we assert two
end-to-end properties that no single unit test can guarantee on its
own:

  1. The four critical identifiers (``m2m_app_id``, ``spa_client_id``,
     ``api_resource``, ``user_sub``) on the two success envelopes are
     all pairwise distinct. This is the smoke check that orchestrator
     state from one app cannot leak into the next.

  2. The *actual HTTP bodies* sent to Logto carry distinct
     ``name``/``indicator``/``username`` fields per spec R15 — verifying
     output-envelope distinctness alone could be fooled by a bug that
     correctly relabels the response but POSTs the same body twice.

We also pin one performance invariant: the management token is minted
exactly once across both runs. The bootstrap admin credential is the
same bytearray (one decrypt), and the on-disk mgmt-token cache (file
mode 0600, ``filelock``-guarded) should make the second
``provision_app`` call a cache hit. A regression here would silently
double the shared Logto OIDC traffic on every bulk-scaffold session.

Stubbing strategy:

  - **Keystore (in-memory):** monkeypatch ``keystore.resolve_keystore_path``,
    ``load_keystore``, ``read_passphrase``, and ``decrypt_credential`` to
    skip the real eth-account scrypt path and inject a fresh
    ``bytearray`` admin credential on each call. The orchestrator
    zeroes the buffer after ``provision_app`` returns, so each run
    needs its own buffer — that's what triggered SEC-006 caveat in the
    keystore module.

  - **Logto (real HTTP via ``responses``):** stub every endpoint
    ``logto.provision_app`` touches with per-slug responses. The
    orchestrator's real ``logto.provision_app`` runs end-to-end (find →
    POST → re-fetch on 409). This is the path R15 needs us to inspect.

  - **GitHub / Railway:** disabled via ``with_railway=False,
    with_github=False`` rather than stubbed. That keeps this test
    focused on the Logto layer; the wrapper modules have their own
    unit suites.

  - **Template render:** mocked to a no-op. Copier rendering is
    expensive and unrelated to this test's contract; the unit suite
    ``tests/integration/test_generated_app.py`` covers real rendering.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
import responses

from agentic_core.bootstrap import orchestrator
from agentic_core.bootstrap import logto as logto_mod


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LOGTO_ENDPOINT = "https://logto.test.example.com"
PLATFORM_DOMAIN = "example.test"
ADMIN_M2M_ID = "admin-m2m-id"
ADMIN_M2M_SECRET = "admin-m2m-secret-998877"


def _admin_cred() -> bytearray:
    """Fresh bytearray per call — the orchestrator zeros it post-flight."""
    return bytearray(ADMIN_M2M_SECRET, "utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_token_cache(tmp_path, monkeypatch):
    """Redirect the mgmt-token cache to ``tmp_path`` so two runs in this
    test share a cache without touching the real ``~/.cache``.

    The orchestrator constructs a fresh ``LogtoManagementClient`` per
    ``provision_app`` invocation; cache sharing happens at the file
    level. Pointing both runs at the same tmp file is what lets the
    second run hit the cache instead of re-minting.
    """
    cache_path = tmp_path / "cache" / "agentic-core" / "mgmt-token.json"
    monkeypatch.setattr(logto_mod, "DEFAULT_MGMT_TOKEN_CACHE", cache_path)
    # Backoff knobs to zero — no test should sit waiting on the (unused)
    # 429 retry path.
    monkeypatch.setattr(logto_mod, "BACKOFF_BASE_SECONDS", 0.0)
    monkeypatch.setattr(logto_mod, "BACKOFF_MAX_SECONDS", 0.0)
    monkeypatch.setattr(logto_mod, "BACKOFF_JITTER_FRACTION", 0.0)
    yield cache_path


@pytest.fixture
def patched_orchestrator(monkeypatch, tmp_path):
    """Patch keystore + template so only the Logto HTTP layer is live.

    Returns a dict of mocks for tests that want to assert call counts.
    """
    # Keystore: skip the real scrypt decrypt and return a fresh
    # bytearray that the orchestrator can hand to logto.provision_app.
    # Returning a *fresh* bytearray each call matches the real keystore
    # contract (the orchestrator zeros what it gets back) — sharing one
    # bytearray across calls would crash the second run with a zeroed
    # client_secret.
    monkeypatch.setattr(
        orchestrator.keystore,
        "resolve_keystore_path",
        mock.MagicMock(return_value=tmp_path / "admin.keystore.json"),
    )
    monkeypatch.setattr(
        orchestrator.keystore,
        "load_keystore",
        mock.MagicMock(return_value={"crypto": {}, "version": 3}),
    )
    monkeypatch.setattr(
        orchestrator.keystore,
        "read_passphrase",
        mock.MagicMock(return_value="passphrase-not-used"),
    )
    decrypt_mock = mock.MagicMock(side_effect=lambda *_a, **_kw: _admin_cred())
    monkeypatch.setattr(
        orchestrator.keystore, "decrypt_credential", decrypt_mock
    )

    # Template: skip Copier rendering — out of scope for this test.
    render_mock = mock.MagicMock(return_value=None)
    monkeypatch.setattr(orchestrator.template, "render_template", render_mock)

    # Preflight (§R6/SC4) runs BEFORE any external call and checks that the
    # admin keystore file EXISTS + is readable (it does not decrypt it —
    # decrypt_credential is stubbed above). This test injects Logto via
    # `responses` stubs with no real keystore on disk, so drop a placeholder at
    # the path the orchestrator points preflight at; otherwise preflight aborts
    # the run with PREFLIGHT_KEYSTORE_MISSING before provision_app ever fires.
    (tmp_path / "admin.keystore.json").write_bytes(b"{}")

    return {"decrypt": decrypt_mock, "render_template": render_mock}


def _stub_logto_for_slug(
    slug: str,
    *,
    spa_id: str,
    m2m_id: str,
    m2m_secret: str,
    resource_id: str,
    user_id: str,
) -> str:
    """Register the ``responses`` stubs for one ``provision_app`` slug.

    Returns the indicator URL so the test can cross-check against the
    success envelope. The stubs are registered in the same order the
    orchestrator-driven ``provision_app`` will hit them (find_* first,
    then create_*) so a regression that reorders calls would still match
    but a regression that *adds* an unstubbed call would fail loudly with
    ``responses.ConnectionError``.
    """
    indicator = f"https://{slug}.{PLATFORM_DOMAIN}/api"

    # find_application(name=<slug>-frontend) → empty (proceed to POST)
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/applications",
        json=[],
        status=200,
        match=[
            responses.matchers.query_param_matcher(
                {"search": f"{slug}-frontend"}
            )
        ],
    )
    # find_application(name=<slug>-agent) → empty
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/applications",
        json=[],
        status=200,
        match=[
            responses.matchers.query_param_matcher(
                {"search": f"{slug}-agent"}
            )
        ],
    )
    # find_resource(indicator) → empty
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/resources",
        json=[],
        status=200,
        match=[responses.matchers.query_param_matcher({"search": indicator})],
    )
    # find_user(username=app_<slug>) → empty
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/users",
        json=[],
        status=200,
        match=[
            responses.matchers.query_param_matcher(
                {"search": f"app_{slug.replace('-', '_')}"}
            )
        ],
    )

    # create SPA application
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/applications",
        json={"id": spa_id, "name": f"{slug}-frontend", "type": "SPA"},
        status=200,
    )
    # create M2M application
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
    # create API resource
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/resources",
        json={
            "id": resource_id,
            "indicator": indicator,
            "name": f"{slug} API",
        },
        status=200,
    )
    # create scopes (read + write)
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
    # create per-app user
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/users",
        json={"id": user_id, "username": f"app_{slug.replace('-', '_')}"},
        status=200,
    )
    # Roles: scopes on the resource are inert until a role carries them, so
    # provisioning creates one User role + one MachineToMachine role per app
    # and assigns the user / M2M client to them.
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/roles",
        json=[],
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/roles",
        json={"id": f"role-user-{slug}", "name": f"{slug}-user", "type": "User"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/roles",
        json={
            "id": f"role-agent-{slug}",
            "name": f"{slug}-agent",
            "type": "MachineToMachine",
        },
        status=200,
    )
    for rid in (f"role-user-{slug}", f"role-agent-{slug}"):
        responses.add(
            responses.GET, f"{LOGTO_ENDPOINT}/api/roles/{rid}/users", json=[], status=200
        )
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/roles/{rid}/applications",
            json=[],
            status=200,
        )
        responses.add(
            responses.POST, f"{LOGTO_ENDPOINT}/api/roles/{rid}/users", json={}, status=201
        )
        responses.add(
            responses.POST,
            f"{LOGTO_ENDPOINT}/api/roles/{rid}/applications",
            json={},
            status=201,
        )
    return indicator


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@responses.activate
def test_two_consecutive_create_app_runs_produce_distinct_credentials(
    tmp_path, monkeypatch, isolated_token_cache, patched_orchestrator
):
    """Run ``orchestrate_create_app`` for ``foo`` then ``bar`` and assert
    that:

      - Both success envelopes carry distinct ``m2m_app_id``,
        ``spa_client_id``, ``api_resource``, ``user_sub`` (the four
        fields that key the per-app Logto footprint).
      - The HTTP bodies sent for the two runs are distinct, per spec
        R15: parses ``responses.calls`` and asserts on the JSON payload
        ``name``/``indicator``/``username`` fields.
      - The mgmt token endpoint is called exactly once across both runs
        (cache hit on the second app's mint cycle).
    """
    # Stub the OIDC token endpoint ONCE — we expect the second
    # provision_app to reuse the cached token, so a second stub would
    # be unused (and a regression that re-mints would fail with
    # ConnectionError because we did not stub a second response).
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/oidc/token",
        json={
            "access_token": "mgmt-token-once",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
        status=200,
    )

    _stub_logto_for_slug(
        "foo",
        spa_id="spa-foo-001",
        m2m_id="m2m-foo-001",
        m2m_secret="secret-foo-001",
        resource_id="res-foo-001",
        user_id="user-foo-001",
    )
    _stub_logto_for_slug(
        "bar",
        spa_id="spa-bar-002",
        m2m_id="m2m-bar-002",
        m2m_secret="secret-bar-002",
        resource_id="res-bar-002",
        user_id="user-bar-002",
    )

    common_kwargs: dict = dict(
        variant="full",
        with_railway=False,
        with_github=False,
        with_logto=True,
        with_auth=True,
        keystore_path=tmp_path / "admin.keystore.json",
        platform_domain=PLATFORM_DOMAIN,
        logto_endpoint=LOGTO_ENDPOINT,
        admin_app_id=ADMIN_M2M_ID,
        scaffolded_by="agent",
    )

    foo_envelope = orchestrator.orchestrate_create_app(
        slug="foo",
        description="foo app",
        output_dir=tmp_path / "out-foo",
        state_path=tmp_path / "out-foo" / ".bootstrap-state.json",
        **common_kwargs,
    )
    bar_envelope = orchestrator.orchestrate_create_app(
        slug="bar",
        description="bar app",
        output_dir=tmp_path / "out-bar",
        state_path=tmp_path / "out-bar" / ".bootstrap-state.json",
        **common_kwargs,
    )

    # ------------------------------------------------------------------
    # Acceptance criterion 1: distinct envelope identifiers
    # ------------------------------------------------------------------
    foo_logto = foo_envelope["logto"]
    bar_logto = bar_envelope["logto"]
    assert foo_envelope["status"] == "success"
    assert bar_envelope["status"] == "success"

    assert foo_logto["m2m_app_id"] != bar_logto["m2m_app_id"], (
        "m2m_app_id leaked across runs: "
        f"{foo_logto['m2m_app_id']!r} == {bar_logto['m2m_app_id']!r}"
    )
    assert foo_logto["spa_client_id"] != bar_logto["spa_client_id"], (
        "spa_client_id leaked across runs: "
        f"{foo_logto['spa_client_id']!r} == {bar_logto['spa_client_id']!r}"
    )
    assert foo_logto["api_resource"] != bar_logto["api_resource"], (
        "api_resource leaked across runs: "
        f"{foo_logto['api_resource']!r} == {bar_logto['api_resource']!r}"
    )
    assert foo_logto["user_sub"] != bar_logto["user_sub"], (
        "user_sub leaked across runs: "
        f"{foo_logto['user_sub']!r} == {bar_logto['user_sub']!r}"
    )

    # Cross-check the concrete values the stubs returned — protects
    # against a regression where the orchestrator drops the per-call
    # ``logto`` dict and reuses a captured one.
    assert foo_logto["m2m_app_id"] == "m2m-foo-001"
    assert bar_logto["m2m_app_id"] == "m2m-bar-002"
    assert foo_logto["spa_client_id"] == "spa-foo-001"
    assert bar_logto["spa_client_id"] == "spa-bar-002"
    assert foo_logto["api_resource"] == f"https://foo.{PLATFORM_DOMAIN}/api"
    assert bar_logto["api_resource"] == f"https://bar.{PLATFORM_DOMAIN}/api"
    assert foo_logto["user_sub"] == "app_foo"
    assert bar_logto["user_sub"] == "app_bar"

    # ------------------------------------------------------------------
    # Acceptance criterion 2 (R15): distinct POST bodies in HTTP captures
    # ------------------------------------------------------------------
    def _post_bodies(path_substr: str) -> list[dict]:
        out: list[dict] = []
        for call in responses.calls:
            if call.request.method != "POST":
                continue
            url = call.request.url
            # Filter out scope-create POSTs that look like
            # /api/resources/<id>/scopes — those carry scope payloads,
            # not resource payloads.
            if path_substr == "/api/resources" and "/scopes" in url:
                continue
            if path_substr not in url:
                continue
            body = call.request.body
            if isinstance(body, bytes):
                body = body.decode()
            try:
                out.append(json.loads(body))
            except (TypeError, json.JSONDecodeError):
                # OIDC token endpoint posts form data, not JSON. Skip;
                # the per-endpoint asserts only inspect JSON bodies.
                continue
        return out

    # /api/applications: 2 apps × (SPA + M2M) = 4 POSTs total
    app_bodies = _post_bodies("/api/applications")
    assert len(app_bodies) == 4, (
        f"expected 4 POSTs to /api/applications across two runs, got "
        f"{len(app_bodies)}: {app_bodies!r}"
    )
    app_names = sorted(b["name"] for b in app_bodies)
    assert app_names == [
        "bar-agent",
        "bar-frontend",
        "foo-agent",
        "foo-frontend",
    ], (
        f"/api/applications POST `name` payloads were not the expected "
        f"set: {app_names!r}"
    )
    # All four names distinct.
    assert len(set(app_names)) == 4

    # /api/resources: 2 POSTs (one per app), with distinct indicator
    resource_bodies = _post_bodies("/api/resources")
    assert len(resource_bodies) == 2, (
        f"expected 2 POSTs to /api/resources, got {len(resource_bodies)}"
    )
    indicators = sorted(b["indicator"] for b in resource_bodies)
    assert indicators == [
        f"https://bar.{PLATFORM_DOMAIN}/api",
        f"https://foo.{PLATFORM_DOMAIN}/api",
    ], f"/api/resources indicator payloads: {indicators!r}"

    # /api/users: 2 POSTs (one per app), with distinct username
    user_bodies = _post_bodies("/api/users")
    assert len(user_bodies) == 2, (
        f"expected 2 POSTs to /api/users, got {len(user_bodies)}"
    )
    usernames = sorted(b["username"] for b in user_bodies)
    assert usernames == ["app_bar", "app_foo"], (
        f"/api/users username payloads: {usernames!r}"
    )

    # ------------------------------------------------------------------
    # Acceptance criterion 3: mgmt token cache hit on 2nd run
    # ------------------------------------------------------------------
    # Exactly ONE POST to /oidc/token across both provision_app calls.
    # Cache lives on disk at isolated_token_cache; the second
    # provision_app's LogtoManagementClient observes a fresh token and
    # skips the mint.
    token_posts = [
        c
        for c in responses.calls
        if c.request.method == "POST" and "/oidc/token" in c.request.url
    ]
    assert len(token_posts) == 1, (
        f"expected exactly 1 mgmt token mint across both runs (cache hit "
        f"on 2nd run), got {len(token_posts)}. The token cache at "
        f"{isolated_token_cache} should be shared across "
        f"LogtoManagementClient instances."
    )

    # Cache file actually got written (defends against a regression where
    # the second run hits cache by accident because the first run also
    # silently failed to persist it).
    assert isolated_token_cache.exists(), (
        f"token cache file was not written at {isolated_token_cache}"
    )
