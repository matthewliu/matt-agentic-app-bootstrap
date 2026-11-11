"""Roles must be provisioned, or every minted token comes back scope-less.

`create_scopes` puts `read`/`write` on the per-app API resource, but a scope
that no role carries is never emitted in a token. Between the introduction of
per-app provisioning and this test, `provision_app` created scopes and stopped
— so every generated app's middleware 403'd on its required-scope check, for
both user and M2M callers. The sibling apps that worked had their roles created
by hand.

Logto keeps User and MachineToMachine roles in separate namespaces: a User role
cannot be assigned to an application and vice versa. An app that serves both
humans (SPA) and agents (CLI) therefore needs one of each.
"""

from __future__ import annotations

import json

import responses
from responses import matchers

from agentic_core.bootstrap import logto

LOGTO_ENDPOINT = "https://logto.test.example.com"
ADMIN_M2M_ID = "admin-m2m-id"
SLUG = "role-test-app"
INDICATOR = f"https://{SLUG}.example.dev/api"
RESOURCE_ID = "res-id"
SPA_ID = "spa-id"
M2M_ID = "m2m-id"
USER_ID = "usr-id"
READ_SCOPE_ID = "scope-read"
WRITE_SCOPE_ID = "scope-write"
USER_ROLE_ID = "role-user-id"
AGENT_ROLE_ID = "role-agent-id"


def _stub_everything() -> None:
    """Stub the whole provisioning happy path, roles included."""
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/oidc/token",
        json={"access_token": "mgmt-token", "expires_in": 3600, "token_type": "Bearer"},
        status=200,
    )
    # Finders return empty -> everything gets created.
    for name in (f"{SLUG}-frontend", f"{SLUG}-agent"):
        responses.add(
            responses.GET,
            f"{LOGTO_ENDPOINT}/api/applications",
            json=[],
            status=200,
            match=[matchers.query_param_matcher({"search": name})],
        )
    responses.add(
        responses.GET,
        f"{LOGTO_ENDPOINT}/api/resources",
        json=[],
        status=200,
        match=[matchers.query_param_matcher({"search": INDICATOR})],
    )
    responses.add(responses.GET, f"{LOGTO_ENDPOINT}/api/users", json=[], status=200)

    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/applications",
        json={"id": SPA_ID, "name": f"{SLUG}-frontend", "type": "SPA"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/applications",
        json={
            "id": M2M_ID,
            "name": f"{SLUG}-agent",
            "type": "MachineToMachine",
            "secret": "m2m-sec",
        },
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/resources",
        json={"id": RESOURCE_ID, "indicator": INDICATOR, "name": f"{SLUG} API"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/resources/{RESOURCE_ID}/scopes",
        json={"id": READ_SCOPE_ID, "name": "read"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/resources/{RESOURCE_ID}/scopes",
        json={"id": WRITE_SCOPE_ID, "name": "write"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/users",
        json={"id": USER_ID, "username": f"app_{SLUG.replace('-', '_')}"},
        status=200,
    )

    # Roles: find_role sees an empty tenant, then each create returns an id.
    responses.add(responses.GET, f"{LOGTO_ENDPOINT}/api/roles", json=[], status=200)
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/roles",
        json={"id": USER_ROLE_ID, "name": f"{SLUG}-user", "type": "User"},
        status=200,
    )
    responses.add(
        responses.POST,
        f"{LOGTO_ENDPOINT}/api/roles",
        json={"id": AGENT_ROLE_ID, "name": f"{SLUG}-agent", "type": "MachineToMachine"},
        status=200,
    )
    for rid in (USER_ROLE_ID, AGENT_ROLE_ID):
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


def _provision(**kwargs):
    return logto.provision_app(
        slug=SLUG,
        endpoint=LOGTO_ENDPOINT,
        admin_app_id=ADMIN_M2M_ID,
        admin_credential=bytearray(b"admin-secret"),
        platform_domain="example.dev",
        **kwargs,
    )


def _role_creates() -> list[dict]:
    return [
        json.loads(c.request.body)
        for c in responses.calls
        if c.request.method == "POST" and c.request.url.rstrip("/").endswith("/api/roles")
    ]


class TestRoleProvisioning:
    @responses.activate
    def test_creates_one_role_per_principal_kind(self):
        _stub_everything()
        result = _provision()

        created = _role_creates()
        assert len(created) == 2, f"expected a User and an M2M role, got {created}"

        by_type = {r["type"]: r for r in created}
        assert set(by_type) == {"User", "MachineToMachine"}
        assert by_type["User"]["name"] == f"{SLUG}-user"
        assert by_type["MachineToMachine"]["name"] == f"{SLUG}-agent"

        assert result["user_role_id"] == USER_ROLE_ID
        assert result["agent_role_id"] == AGENT_ROLE_ID

    @responses.activate
    def test_both_roles_carry_every_resource_scope(self):
        """A role without the scopes is the same as no role at all."""
        _stub_everything()
        _provision()

        for role in _role_creates():
            assert sorted(role["scopeIds"]) == sorted([READ_SCOPE_ID, WRITE_SCOPE_ID]), role

    @responses.activate
    def test_assigns_user_to_user_role_and_app_to_agent_role(self):
        _stub_everything()
        _provision()

        posts = {
            c.request.url.split("/api/", 1)[1]: json.loads(c.request.body)
            for c in responses.calls
            if c.request.method == "POST" and "/api/roles/" in c.request.url
        }
        assert posts[f"roles/{USER_ROLE_ID}/users"] == {"userIds": [USER_ID]}
        assert posts[f"roles/{AGENT_ROLE_ID}/applications"] == {"applicationIds": [M2M_ID]}
        # Cross-assignment is impossible in Logto and must not be attempted.
        assert f"roles/{USER_ROLE_ID}/applications" not in posts
        assert f"roles/{AGENT_ROLE_ID}/users" not in posts

    @responses.activate
    def test_returns_user_id_distinct_from_username(self):
        """`user_id` is what a browser login puts in `sub`; `user_sub` is the
        username teardown resolves by. Conflating them gives one human two
        UserProfile rows — one from the SPA, one from the CLI."""
        _stub_everything()
        result = _provision()

        assert result["user_id"] == USER_ID
        assert result["user_sub"] == f"app_{SLUG.replace('-', '_')}"
        assert result["user_id"] != result["user_sub"]

    @responses.activate
    def test_no_user_means_no_user_role(self):
        _stub_everything()
        _provision(with_user=False)

        created = _role_creates()
        assert [r["type"] for r in created] == ["MachineToMachine"], created

    @responses.activate
    def test_no_m2m_means_no_agent_role(self):
        _stub_everything()
        _provision(with_m2m=False)

        created = _role_creates()
        assert [r["type"] for r in created] == ["User"], created

    @responses.activate
    def test_no_api_resource_means_no_roles(self):
        """No resource -> no scopes -> a role would carry nothing."""
        _stub_everything()
        _provision(with_m2m=False, with_api_resource=False)

        assert _role_creates() == []
