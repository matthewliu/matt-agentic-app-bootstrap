"""The provisioner and the generated app must agree. Nothing here is mocked.

Every auth bug this repo has shipped was the same shape: the Copier template
and `agentic_core.bootstrap` each hard-coded their half of a shared value, the
two halves drifted, and both sides' unit tests kept passing because each mocked
the other. Five of them at once:

  1. Template expected `aud = https://api.<slug>.<domain>`; the provisioner
     created `https://<slug>.<domain>/api`. Every token failed its audience
     check.
  2. Middleware required scope `<slug>:api`; `create_scopes` only ever makes
     `read` and `write`. Every caller 403'd.
  3. Frontend requested `read:<pkg>`/`write:<pkg>` — also nonexistent.
  4. `provision_app` created scopes but no roles, so no principal held any
     scope and every token came back with an empty `scope` claim.
  5. The orchestrator never wrote the actor-sub allowlist the middleware reads,
     and wrote the actor identity as a username where the middleware compares
     against the user id a browser login puts in `sub`.

So these tests render the REAL template and compare it against the REAL
provisioner constants. A unit test cannot catch this class of bug by
construction — only a comparison can. When a contract value legitimately
changes, change it in `logto.py` and these tests follow automatically; the
generated app is what must be made to agree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agentic_core.bootstrap import logto

SLUG = "contract-app"
DOMAIN = "example.dev"


@pytest.fixture(scope="module")
def app_dir(tmp_path_factory) -> Path:
    """Render the template once for the whole module."""
    import subprocess

    from tests.conftest import COPIER_BIN, TEMPLATE_DIR  # noqa: PLC0415

    dst = tmp_path_factory.mktemp("contract") / "output"
    dst.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            COPIER_BIN, "copy", str(TEMPLATE_DIR), str(dst),
            "--trust", "--skip-tasks", "--defaults",
            "--data", f"app_name={SLUG}",
            "--data", "app_description=contract fixture",
            "--data", "github_user=testuser",
            "--data", f"platform_domain={DOMAIN}",
            "--data", "include_frontend=true",
        ],
        check=True, capture_output=True, text=True,
    )
    return dst / SLUG


def _read(app_dir: Path, rel: str) -> str:
    p = app_dir / rel
    assert p.exists(), f"generated app is missing {rel}"
    return p.read_text()


# ---------------------------------------------------------------------------
# 1. API resource indicator
# ---------------------------------------------------------------------------


class TestApiResourceIndicator:
    def test_backend_default_matches_provisioner(self, app_dir):
        expected = logto.api_resource_indicator(SLUG, DOMAIN)
        cfg = _read(app_dir, "app/config.py")
        m = re.search(r'logto_api_resource: str = "([^"]+)"', cfg)
        assert m, "app/config.py has no logto_api_resource default"
        assert m.group(1) == expected

    def test_env_example_matches_provisioner(self, app_dir):
        expected = logto.api_resource_indicator(SLUG, DOMAIN)
        env = _read(app_dir, ".env.example")
        m = re.search(r"^LOGTO_API_RESOURCE=(.+)$", env, re.M)
        assert m, ".env.example has no LOGTO_API_RESOURCE"
        assert m.group(1).strip() == expected

    def test_test_fixtures_match_provisioner(self, app_dir):
        """A conftest with the wrong aud makes the app's own tests lie."""
        expected = logto.api_resource_indicator(SLUG, DOMAIN)
        conftest = _read(app_dir, "tests/conftest.py")
        for found in re.findall(r'"(https://[^"]*api[^"]*)"', conftest):
            assert found == expected, f"conftest uses {found!r}, provisioner makes {expected!r}"


# ---------------------------------------------------------------------------
# 2 + 3. Scope names
# ---------------------------------------------------------------------------


class TestScopeNames:
    def test_required_scope_is_one_the_provisioner_creates(self, app_dir):
        mw = _read(app_dir, "app/auth/middleware.py")
        m = re.search(r'DEFAULT_REQUIRED_SCOPE = "([^"]+)"', mw)
        assert m, "middleware has no literal DEFAULT_REQUIRED_SCOPE"
        assert m.group(1) in logto.DEFAULT_SCOPES, (
            f"middleware requires {m.group(1)!r}, but create_scopes only makes "
            f"{list(logto.DEFAULT_SCOPES)} — every request would 403"
        )

    def test_floor_scope_constant_is_creatable(self):
        assert logto.FLOOR_SCOPE in logto.DEFAULT_SCOPES

    def test_frontend_requests_only_real_scopes(self, app_dir):
        cfg = _read(app_dir, "frontend/src/config.ts")
        m = re.search(r"scopes:\s*\[([^\]]*)\]", cfg)
        assert m, "frontend config.ts has no scopes array"
        requested = re.findall(r'"([^"]+)"', m.group(1))
        assert requested, "frontend requests no scopes at all"
        unknown = [s for s in requested if s not in logto.DEFAULT_SCOPES]
        assert not unknown, (
            f"frontend requests {unknown} which Logto never creates; the token "
            f"comes back without them and the backend 403s"
        )


# ---------------------------------------------------------------------------
# 4. Roles
# ---------------------------------------------------------------------------


class TestRoles:
    def test_provisioner_exposes_both_role_names(self):
        user_role, agent_role = logto.role_names(SLUG)
        assert user_role == f"{SLUG}-user"
        assert agent_role == f"{SLUG}-agent"

    def test_provision_app_creates_roles(self):
        """Guards the regression where scopes were created but never granted."""
        import inspect

        src = inspect.getsource(logto.provision_app)
        assert "create_role" in src, (
            "provision_app creates scopes but never grants them — a scope no "
            "role carries is never emitted in a token"
        )
        assert "assign_role_to_user" in src
        assert "assign_role_to_application" in src


# ---------------------------------------------------------------------------
# 5. Env var names — the orchestrator writes them, the app reads them
# ---------------------------------------------------------------------------


class TestEnvVarNames:
    @staticmethod
    def _orchestrator_env_keys() -> str:
        return (
            Path(logto.__file__).parent / "orchestrator.py"
        ).read_text()

    def test_actor_sub_allowlist_is_written_by_the_orchestrator(self, app_dir):
        """The middleware fails closed on an empty allowlist, correctly — so
        the orchestrator has to seed it or no agent call ever succeeds."""
        mw = _read(app_dir, "app/auth/middleware.py")
        m = re.search(r'ALLOWLIST_ENV = f?"?\{APP_UPPER\}_(M2M_ACTOR_SUBS)"?', mw)
        assert m, "middleware no longer derives ALLOWLIST_ENV as expected"
        assert "_M2M_ACTOR_SUBS" in self._orchestrator_env_keys(), (
            "middleware reads {APP}_M2M_ACTOR_SUBS but the orchestrator never "
            "writes it — every M2M call 403s on a fresh app"
        )

    def test_backend_settings_names_are_written_by_the_orchestrator(self, app_dir):
        """config.py has no env_prefix, so it reads these exact bare names."""
        orch = self._orchestrator_env_keys()
        for name in ("LOGTO_ENDPOINT", "LOGTO_API_RESOURCE", "LOGTO_M2M_AGENT_APP_ID"):
            assert f'"{name}"' in orch, f"orchestrator never sets {name}"

    def test_actor_identity_is_the_user_id_not_the_username(self):
        """A browser login puts the user *id* in `sub`. If the CLI sends the
        username as its actor-sub, the same human gets two UserProfile rows."""
        orch = self._orchestrator_env_keys()
        m = re.search(r'\(f"\{app_upper\}_USER_SUB", (\w+)\)', orch)
        assert m, "orchestrator no longer sets {APP}_USER_SUB"
        assert m.group(1) == "user_id_val", (
            f"{{APP}}_USER_SUB is set from {m.group(1)!r}; it must be the Logto "
            f"user id, not the username"
        )


# ---------------------------------------------------------------------------
# 6. Install commands must agree across the three places that install
# ---------------------------------------------------------------------------


class TestPnpmInstallConsistency:
    def test_dockerfile_ci_and_eas_use_the_same_flag(self, app_dir):
        """pnpm turns --frozen-lockfile on by default when CI=true, so a
        disagreement here shows up only in CI or only in the Docker build."""
        dockerfile = _read(app_dir, "Dockerfile")
        ci = _read(app_dir, ".github/workflows/ci.yml")

        def install_flag(text: str) -> str | None:
            m = re.search(r"pnpm install(?: (--(?:no-)?frozen-lockfile))?", text)
            return (m.group(1) or "bare") if m else None

        assert install_flag(dockerfile) == install_flag(ci), (
            f"Dockerfile uses {install_flag(dockerfile)!r} but CI uses "
            f"{install_flag(ci)!r}"
        )

    def test_setup_node_cache_only_if_a_lockfile_ships(self, app_dir):
        """`cache: "pnpm"` makes setup-node derive a key from a lockfile and
        hard-error when there is none — before install even runs."""
        ci = _read(app_dir, ".github/workflows/ci.yml")
        caches_pnpm = re.search(r'^\s*cache:\s*["\']pnpm["\']', ci, re.M) is not None
        assert caches_pnpm == (app_dir / "pnpm-lock.yaml").exists(), (
            "CI enables setup-node's pnpm cache but no pnpm-lock.yaml ships"
        )

    def test_frozen_lockfile_only_if_a_lockfile_ships(self, app_dir):
        """--frozen-lockfile with no committed lockfile is ERR_PNPM_NO_LOCKFILE."""
        ships_lockfile = (app_dir / "pnpm-lock.yaml").exists()
        dockerfile = _read(app_dir, "Dockerfile")
        uses_frozen = re.search(r"pnpm install --frozen-lockfile", dockerfile) is not None
        assert uses_frozen == ships_lockfile, (
            "the template must either commit a pnpm-lock.yaml and use "
            "--frozen-lockfile, or ship neither"
        )


# ---------------------------------------------------------------------------
# 7. The template must stay updatable
# ---------------------------------------------------------------------------


class TestUpdatability:
    def test_answers_file_lands_inside_the_app(self, app_dir):
        """Without this, `copier update` cannot run and no template fix can
        ever reach an already-generated app."""
        assert (app_dir / ".copier-answers.yml").exists(), (
            ".copier-answers.yml is not in the generated app — it is probably "
            "at the destination root, i.e. in the shared parent directory that "
            "holds every sibling app, where the next create-app overwrites it"
        )

    def test_answers_file_records_every_question(self, app_dir):
        """A hand-maintained subset silently drops questions added later, and
        `copier update` then re-answers them from defaults."""
        import yaml

        answers = yaml.safe_load((app_dir / ".copier-answers.yml").read_text())
        for q in ("app_name", "variant", "with_auth", "with_db",
                  "include_frontend", "platform_domain", "deploy_target"):
            assert q in answers, f"{q} missing from .copier-answers.yml"
