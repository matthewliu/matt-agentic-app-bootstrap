"""Final ``verify()`` step for the bootstrap orchestrator (U7 / R5 / R8).

After ``STEP_RAILWAY_DEPLOY`` the orchestrator must confirm the deployed app
actually serves traffic and its auth works BEFORE declaring the run a success
(R5) — the pipeline previously reported success while producing a broken app.
:func:`verify_deployment` does two things against the live prod URL:

1. Poll ``GET <base_url>/health`` until HTTP 200, bounded to
   ``max_health_attempts`` with exponential backoff (default budget ~5 minutes
   of bounded wait). Health not yet up within the bound (or a network error) is
   **TRANSIENT** — the Railway deploy is async and DETACHED (``railway up -d``
   returns as soon as the build is accepted and does NOT wait for health), so a
   multi-minute Dockerfile build + rollout can legitimately exhaust the poll.

2. Mint an M2M access token for the app (using the app's OWN provisioned M2M
   client creds) and make ONE authenticated request, then classify the status
   per the template middleware contract:

   - **200** (accepted + route ok) and **403** (JWT VALID but actor-sub not
     allowlisted — the JWT *was* validated) → auth works, **SUCCESS**.
   - **401** (invalid JWT) and **503** (middleware fails closed because
     ``LOGTO_M2M_AGENT_APP_ID`` is unset) and **404** (wrong route/probe) →
     **STRUCTURAL**: a redeploy / config fix is needed; re-running won't help.
   - **429 / 5xx** (rate limit / slow rollout) → **TRANSIENT**: resume re-checks.

   The old "any non-401 = accepted" logic rubber-stamped a 503 (a backend with
   no Logto config) as success; the per-status table above closes that hole.

Transient-vs-structural is the core of this unit (KTD6). The orchestrator maps
STRUCTURAL → ``_fail(retryable=False)`` (``--resume`` refuses to loop on a
genuinely broken app) and TRANSIENT → ``_fail(retryable=True)`` (resume
re-checks later once the rollout settles).

Purity / injectability: the HTTP client (a ``.get``/``.post`` seam, e.g.
``httpx.Client``) and the ``mint_token`` callable are injected so tests never
touch the network. Tests may instead patch :data:`httpx`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

#: Structured outcome discriminants returned in ``result["status"]``.
SUCCESS = "success"
TRANSIENT = "transient"
STRUCTURAL = "structural"

#: Health-poll bound + backoff defaults. ``railway up -d`` is DETACHED — per
#: the CLI's own help it "does not wait for the deployment to become healthy" —
#: so a first-run Dockerfile build + rollout legitimately takes SEVERAL minutes
#: before ``/health`` answers 200. The old 10-attempt / 8s-cap budget (~1 min
#: total) routinely exhausted before the app came up, flaking verify to
#: TRANSIENT on every first run. These defaults give ~5 minutes of bounded
#: wait: 15 attempts with backoff 1,2,4,8,16 then capped at 30s each ⇒ the 14
#: inter-poll sleeps sum to ~301s. Health-exhausted is already retryable, so
#: over-waiting only costs time, never correctness (the poll is still finite —
#: never an infinite loop). All three remain overridable per call.
DEFAULT_MAX_HEALTH_ATTEMPTS = 15
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_MAX_SECONDS = 30.0

#: HTTP timeout (seconds) for the injected/default client.
DEFAULT_TIMEOUT_SECONDS = 10.0

#: The deployed app's default authenticated probe route. ``/api/items`` is
#: behind the JWT middleware (see the template's ``routes/items.py``): a valid
#: app token yields 403 (missing actor-sub header) — still "accepted" — while a
#: misconfigured deployment yields 401. Either way it exercises the JWT path.
DEFAULT_AUTH_PATH = "/api/items"


class VerifyError(Exception):
    """Raised by a token minter when it cannot obtain an access token."""


def _backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff (no jitter — the sleep fn is injectable in tests)."""
    return min(base * (2**attempt), cap)


def verify_deployment(
    base_url: str,
    *,
    mint_token: Callable[[], str] | None = None,
    health_path: str = "/health",
    auth_path: str = DEFAULT_AUTH_PATH,
    http_client: Any | None = None,
    max_health_attempts: int = DEFAULT_MAX_HEALTH_ATTEMPTS,
    backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    backoff_max: float = DEFAULT_BACKOFF_MAX_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Verify a deployed app: bounded ``/health`` poll + one auth probe.

    Args:
      base_url: Prod origin, e.g. ``https://<slug>-production.up.railway.app``
        (the orchestrator sources this from ``resource_ids['railway_domain']``).
      mint_token: Zero-arg callable returning an app access token. ``None`` runs
        health-only verification (used when the app's M2M secret is not
        available in-process, e.g. a ``--resume`` where ``STEP_LOGTO`` was
        skipped, or a variant with no M2M client).
      health_path: Unauthenticated health route. Default ``/health``.
      auth_path: Authenticated probe route. Default :data:`DEFAULT_AUTH_PATH`.
      http_client: Injected ``.get``/``.post`` client (e.g. ``httpx.Client``).
        ``None`` ⇒ a short-lived :class:`httpx.Client` is created and closed.
      max_health_attempts: Bound on health polls (>= 1).
      backoff_base / backoff_max: Exponential backoff between polls.
      sleep: Injectable sleep (tests pass a no-op).
      timeout: HTTP timeout for the default client.

    Returns:
      ``{"status": SUCCESS, "auth_checked": <bool>}`` on success, or
      ``{"status": TRANSIENT|STRUCTURAL, "error_code": ..., "message": ...}``.
    """
    base = base_url.rstrip("/")
    health_url = base + health_path
    auth_url = base + auth_path

    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.Client(timeout=timeout)
    try:
        # ------------------------------------------------------------------
        # 1) Bounded /health poll — never-200-within-bound is TRANSIENT.
        # ------------------------------------------------------------------
        healthy = False
        last_detail = ""
        attempts = max(1, max_health_attempts)
        for attempt in range(attempts):
            try:
                resp = client.get(health_url)
            except Exception as exc:  # noqa: BLE001 — network error = transient
                last_detail = f"network error: {exc}"
            else:
                if resp.status_code == 200:
                    healthy = True
                    break
                last_detail = f"HTTP {resp.status_code}"
            if attempt < attempts - 1:
                sleep(_backoff(attempt, backoff_base, backoff_max))

        if not healthy:
            # Slow rollout / app never came up within the bound. Transient so a
            # later --resume re-checks — a genuinely-dead app then needs
            # operator attention, but resume is operator-invoked, not automatic,
            # so this can never infinite-loop on its own.
            return {
                "status": TRANSIENT,
                "error_code": "VERIFY_HEALTH_UNAVAILABLE",
                "message": (
                    f"{health_url} never returned 200 within "
                    f"{attempts} attempts (last: {last_detail})"
                ),
            }

        # ------------------------------------------------------------------
        # 2) Auth probe (only when app M2M creds are available in-process).
        # ------------------------------------------------------------------
        if mint_token is None:
            return {"status": SUCCESS, "auth_checked": False}

        try:
            token = mint_token()
        except Exception as exc:  # noqa: BLE001 — mint infra blip = transient
            return {
                "status": TRANSIENT,
                "error_code": "VERIFY_TOKEN_MINT_FAILED",
                "message": f"could not mint app access token: {exc}",
            }

        try:
            auth_resp = client.get(
                auth_url, headers={"Authorization": f"Bearer {token}"}
            )
        except Exception as exc:  # noqa: BLE001 — network blip = transient
            return {
                "status": TRANSIENT,
                "error_code": "VERIFY_AUTH_NETWORK_ERROR",
                "message": f"authenticated request to {auth_url} failed: {exc}",
            }

        status = auth_resp.status_code
        # Precise per-status classification per the template middleware contract:
        #   200 → token accepted + route ok
        #   403 → JWT VALID but actor-sub not allowlisted (the JWT WAS validated)
        #   401 → invalid JWT (misconfigured JWKS / aud / azp)
        #   503 → middleware fails closed: its M2M config (LOGTO_M2M_AGENT_APP_ID)
        #         is unset — auth infra is broken
        #   429 / 5xx → slow rollout / transient server error
        #   404 → wrong route / probe path
        # ONLY 200/403 prove auth works. 401/503/404 are STRUCTURAL (a redeploy
        # or config fix is needed — resume won't help). 429/5xx are TRANSIENT.
        if status in (200, 403):
            return {"status": SUCCESS, "auth_checked": True}

        if status == 401:
            # A valid app token rejected by the deployed app ⇒ its JWT config
            # (JWKS / aud / azp) is wrong. Re-running can't fix it → STRUCTURAL.
            return {
                "status": STRUCTURAL,
                "error_code": "VERIFY_AUTH_REJECTED",
                "message": (
                    f"deployed app rejected a valid app token (401) at "
                    f"{auth_url} — JWT middleware is misconfigured"
                ),
            }

        if status == 503:
            # The middleware fails closed with 503 when LOGTO_M2M_AGENT_APP_ID is
            # unset (see the template's auth/middleware.py). Auth infra is broken;
            # a redeploy with the correct backend env is required → STRUCTURAL.
            return {
                "status": STRUCTURAL,
                "error_code": "VERIFY_AUTH_MISCONFIGURED",
                "message": (
                    f"deployed app returned 503 at {auth_url} — auth is "
                    "misconfigured (LOGTO_M2M_AGENT_APP_ID unset); the backend "
                    "needs its Logto env pushed + a redeploy"
                ),
            }

        if status == 404:
            # Wrong route / probe path — auth was never exercised, and re-running
            # can't fix a wrong probe → STRUCTURAL.
            return {
                "status": STRUCTURAL,
                "error_code": "VERIFY_AUTH_ROUTE_MISSING",
                "message": (
                    f"authenticated probe {auth_url} returned 404 — the route is "
                    "missing or the probe path is wrong; auth was not proven"
                ),
            }

        if status == 429 or 500 <= status < 600:
            # Rate limit / server error → transient; a later --resume re-checks.
            return {
                "status": TRANSIENT,
                "error_code": "VERIFY_AUTH_UNAVAILABLE",
                "message": (
                    f"authenticated probe {auth_url} returned HTTP {status} — "
                    "transient (slow rollout / rate limit); auth not yet proven"
                ),
            }

        # Any other status the middleware never emits — not proven → STRUCTURAL.
        return {
            "status": STRUCTURAL,
            "error_code": "VERIFY_AUTH_UNEXPECTED",
            "message": (
                f"authenticated probe {auth_url} returned unexpected HTTP "
                f"{status} — auth was not proven"
            ),
        }
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def make_m2m_token_minter(
    *,
    endpoint: str,
    client_id: str,
    client_secret: str,
    resource: str,
    scope: str = "",
    http_client: Any | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Callable[[], str]:
    """Build a zero-arg minter for an app M2M access token (OAuth2 CC grant).

    Mirrors :meth:`agentic_core.bootstrap.logto.LogtoManagementClient._mint_management_token`
    but targets the app's OWN M2M client + API resource (not the admin client),
    so the resulting token has ``aud == resource`` and ``azp == client_id`` —
    exactly what the deployed app's middleware requires (see the template
    ``auth/middleware.py``). Returned closure raises :class:`VerifyError` if the
    token endpoint responds >= 400.

    The minter creates (and closes) its own short-lived client unless one is
    injected. The app M2M secret it captures is the same value the orchestrator
    already pushed to Railway/GitHub secrets moments earlier — this call reuses
    the in-process value on the fresh run rather than reading it back.
    """
    token_url = endpoint.rstrip("/") + "/oidc/token"

    def _mint() -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "resource": resource,
        }
        if scope:
            data["scope"] = scope
        owns = http_client is None
        client = (
            http_client if http_client is not None else httpx.Client(timeout=timeout)
        )
        try:
            resp = client.post(
                token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except Exception as exc:  # noqa: BLE001
            raise VerifyError(f"token endpoint request failed: {exc}") from exc
        finally:
            if owns:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
        if resp.status_code >= 400:
            raise VerifyError(
                f"token endpoint returned HTTP {resp.status_code} for app "
                f"client {client_id!r}"
            )
        try:
            return resp.json()["access_token"]
        except (KeyError, ValueError) as exc:
            raise VerifyError(f"token endpoint response missing access_token: {exc}") from exc

    return _mint
