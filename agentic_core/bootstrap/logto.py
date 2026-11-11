"""Logto Management API client for agentic-core bootstrap.

Ported from ``scripts/register-logto-app.py`` with three R6 hardening
additions over the original shell-companion script:

1. **Server-side search.** ``list-applications`` and ``list-resources``
   pass ``?search=<name>`` so we never scan the full tenant list. This
   matters once an operator can have 20+ apps and even more so on burst scaffolds.

2. **Exponential backoff with jitter on HTTP 429.** Base 2s, max 32s,
   ±25% jitter, max 5 attempts. After retries are exhausted the client
   raises ``RateLimitError`` (mapped to CLI exit code 6 by the
   orchestrator per spec §R4 / §R6).

3. **Management token caching** at ``~/.cache/agentic-core/mgmt-token.json``
   (file mode 0600, ``filelock`` around read/write to avoid mint storms
   when multiple processes call ``provision_app`` concurrently). Tokens
   are refreshed when ``expires_at - now() < 60``.

The admin Management-API credential is passed in as a ``bytearray``
(the keystore module zeros it after this client finishes). It is used
only as the ``client_secret`` when minting the management token; it is
never logged or persisted to disk.

The public entry point is :func:`provision_app`, which creates the
per-app Logto resources Track C invariants 1, 2, 3 require:

  - One SPA application (per-app frontend OIDC client)
  - One Machine-to-Machine application (per-app agent credential)
  - One API resource at ``https://<slug>.<platform_domain>/api`` with the
    default ``read``/``write`` scopes
  - One per-app Logto user with username ``app_<slug>``

Two consecutive ``provision_app`` calls (e.g. for two different slugs)
always return fully distinct identifiers — that property is asserted in
``tests/unit/test_bootstrap_logto.py::test_two_consecutive_calls_produce_distinct_ids``.
"""

from __future__ import annotations

import email.utils
import json
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Any

from collections.abc import Callable

import requests
from filelock import FileLock


# ---------------------------------------------------------------------------
# Constants & module-level knobs
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# THE PROVISIONER <-> TEMPLATE CONTRACT
#
# Everything in this block is a value the provisioner creates in Logto AND the
# generated app expects to find. They used to be spelled out as literals on
# both sides, which is how the template ended up asking for
# `https://api.<slug>.<domain>` while this module created
# `https://<slug>.<domain>/api`, and requiring a `<slug>:api` scope that
# create_scopes never makes. Both halves passed their own mocked tests for
# months while disagreeing, because no test ever compared them.
#
# Define each value ONCE here. `tests/contract/` renders the real template and
# asserts the generated app agrees with these — mocking neither side.
# ---------------------------------------------------------------------------

#: Scopes created on every per-app API resource, in order.
DEFAULT_SCOPES: tuple[str, ...] = ("read", "write")

#: The scope the generated middleware requires of every caller. Must be one of
#: DEFAULT_SCOPES, or every request 403s on missing_scope.
FLOOR_SCOPE: str = "read"


#: Prefix of each app's dedicated Logto username (``app_<slug>``). Apps made
#: before November 2026 use ``matrix_<slug>``; teardown finds them by the
#: username saved in state, so the old name keeps working.
LOGTO_USER_PREFIX = "app_"


def api_resource_indicator(slug: str, platform_domain: str) -> str:
    """The `aud` value for this app's tokens. Slug first, `/api` suffix.

    The generated app's `logto_api_resource` default must equal this exactly;
    a mismatch fails every token's audience check with no useful error.
    """
    return f"https://{slug}.{platform_domain}/api"


def role_names(slug: str) -> tuple[str, str]:
    """``(user_role, agent_role)``.

    Logto keeps User and MachineToMachine roles in separate namespaces, so an
    app serving both humans and agents needs one of each.
    """
    return (f"{slug}-user", f"{slug}-agent")


#: Logto built-in Management API resource indicator. Same for every tenant.
MANAGEMENT_API_RESOURCE = "https://default.logto.app/api"

#: Default cache file for the management token. Tests monkeypatch this.
DEFAULT_MGMT_TOKEN_CACHE = Path.home() / ".cache" / "agentic-core" / "mgmt-token.json"

#: Refresh the management token this many seconds before its actual expiry.
TOKEN_REFRESH_LEEWAY_SECONDS = 60

#: Exponential-backoff parameters for HTTP 429 handling (spec §R6).
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 32.0
BACKOFF_JITTER_FRACTION = 0.25
MAX_BACKOFF_ATTEMPTS = 5

#: HTTP request timeout (connect, read).
REQUEST_TIMEOUT = (5, 15)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LogtoError(Exception):
    """Base class for all Logto Management API errors."""


class RateLimitError(LogtoError):
    """Raised after MAX_BACKOFF_ATTEMPTS consecutive 429s.

    The orchestrator maps this to CLI exit code 6 (rate-limited, see
    spec §R4).
    """


class LogtoAuthError(LogtoError):
    """Raised on 401 from the OIDC token endpoint (bad admin credential)."""


class LogtoSecretUnrecoverable(LogtoError):
    """Raised when an M2M application's secret cannot be recovered (Fix #15).

    Distinct from the base :class:`LogtoError` (which STEP_LOGTO's generic
    handler treats as ``retryable=True``) so the orchestrator can classify this
    condition as NON-retryable: an adopted or freshly-created M2M app whose
    secret is absent from both the list/create response AND the detail-GET
    fallback will never become recoverable by retrying — the fix is to delete
    the app's bootstrap state and re-create the M2M client from scratch. The
    orchestrator should catch ``LogtoSecretUnrecoverable`` and fail non-retryably.
    """


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------


def _compute_backoff(attempt: int) -> float:
    """Return the sleep duration before retry ``attempt`` (0-indexed).

    Implements spec §R6: base 2s, max 32s, ±25% jitter.
    """
    raw = BACKOFF_BASE_SECONDS * (2**attempt)
    capped = min(raw, BACKOFF_MAX_SECONDS)
    if BACKOFF_JITTER_FRACTION > 0:
        jitter = capped * BACKOFF_JITTER_FRACTION
        capped += random.uniform(-jitter, jitter)
    return max(0.0, capped)


def _parse_retry_after(header_value: str | None) -> float | None:
    """Parse a Retry-After header value (RFC 7231 §7.1.3).

    Supports both delta-seconds (``"5"``) and HTTP-date
    (``"Wed, 21 Oct 2015 07:28:00 GMT"``) forms. Returns the number of
    seconds to wait, clamped to ``[0, BACKOFF_MAX_SECONDS]`` so a
    misbehaving server cannot make us sleep for an hour. Returns
    ``None`` if the header is missing or unparseable, signalling the
    caller to fall back to formulaic exponential backoff.
    """
    if header_value is None:
        return None
    value = header_value.strip()
    if not value:
        return None
    # Delta-seconds form.
    try:
        seconds = float(value)
    except ValueError:
        seconds = None
    if seconds is None:
        # HTTP-date form.
        try:
            parsed = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed is None:
            return None
        seconds = parsed.timestamp() - time.time()
    # Apply jitter so a fleet of clients hitting Retry-After=5 don't
    # all wake up at the same instant.
    if BACKOFF_JITTER_FRACTION > 0 and seconds > 0:
        jitter = seconds * BACKOFF_JITTER_FRACTION
        seconds += random.uniform(-jitter, jitter)
    # Clamp to bound the wait (defends against pathological values).
    return max(0.0, min(seconds, BACKOFF_MAX_SECONDS))


# ---------------------------------------------------------------------------
# Management API client
# ---------------------------------------------------------------------------


class LogtoManagementClient:
    """Thin Management API client with 429 backoff and mgmt-token caching.

    The client is stateless across calls: each public method mints (or
    fetches a cached) management token, makes the call, and returns. The
    file-locked token cache makes concurrent ``provision_app`` calls safe.

    Args:
      endpoint: Logto base URL (e.g. ``https://auth.example.com``).
      admin_app_id: Management API M2M application ID.
      admin_credential: M2M secret as a ``bytearray`` (the keystore zeros
        it after the bootstrap orchestrator finishes; this client only
        reads from it, never copies).
      cache_path: Override for the management token cache location.
        Defaults to :data:`DEFAULT_MGMT_TOKEN_CACHE`.
      session: Optional ``requests.Session`` (for connection reuse).
    """

    def __init__(
        self,
        *,
        endpoint: str,
        admin_app_id: str,
        admin_credential: bytearray,
        cache_path: Path | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.admin_app_id = admin_app_id
        # Hold a reference to the keystore-owned bytearray. The keystore
        # zeros it after the bootstrap orchestrator finishes. We deliberately
        # do NOT eagerly copy to bytes/str here so callers retain control
        # over the credential lifetime.
        #
        # Honest caveat: ``_mint_management_token`` MUST briefly materialize
        # the secret as a Python ``str`` (immutable, non-zeroable) because
        # ``requests`` form-encodes the body via ``urlencode`` which does
        # not accept bytearray values. That str copy lives only on the
        # interpreter's local frame for one ``self._session.request`` call
        # and becomes garbage after the response returns. Residual risk:
        # the interpreter's str interning could keep the secret resident
        # until GC; this is acceptable for the bootstrap path where the
        # process exits within seconds of provisioning.
        self._admin_credential = admin_credential
        self._cache_path_override = cache_path
        self._session = session or requests.Session()

    # ------------------------------------------------------------------
    # Token minting + caching
    # ------------------------------------------------------------------

    def _cache_path(self) -> Path:
        # Re-read the module-level default each call so pytest monkeypatch
        # works even when the client was constructed earlier.
        if self._cache_path_override is not None:
            return self._cache_path_override
        return DEFAULT_MGMT_TOKEN_CACHE

    def _read_cached_token(self) -> str | None:
        """Return a cached token if present + non-expiring within leeway."""
        path = self._cache_path()
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

        token = data.get("access_token")
        expires_at = data.get("expires_at")
        if not isinstance(token, str) or not isinstance(expires_at, (int, float)):
            return None
        if expires_at - time.time() < TOKEN_REFRESH_LEEWAY_SECONDS:
            return None
        return token

    def _write_cached_token(self, access_token: str, expires_at: float) -> None:
        """Atomic 0600 write of the token cache."""
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".mgmt-token.", suffix=".tmp", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {
                        "access_token": access_token,
                        "expires_at": expires_at,
                        "minted_at": time.time(),
                    },
                    f,
                )
            os.rename(tmp, path)
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _mint_management_token(self) -> tuple[str, float]:
        """Acquire a fresh token via the OIDC token endpoint.

        Decodes the bytearray credential only at request time. Returns
        ``(access_token, absolute_expires_at_unix_ts)``.
        """
        url = f"{self.endpoint}/oidc/token"
        # bytes() on a bytearray returns the byte view; decode for the form body.
        secret_str = bytes(self._admin_credential).decode("utf-8")
        data = {
            "grant_type": "client_credentials",
            "client_id": self.admin_app_id,
            "client_secret": secret_str,
            "resource": MANAGEMENT_API_RESOURCE,
            "scope": "all",
        }

        resp = self._request_with_backoff(
            "POST",
            url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            authenticated=False,
        )
        if resp.status_code == 401:
            raise LogtoAuthError(
                "Logto rejected admin credentials (401). Check admin keystore + passphrase."
            )
        if resp.status_code >= 400:
            raise LogtoError(
                f"OIDC token endpoint returned HTTP {resp.status_code}: {resp.text}"
            )
        payload = resp.json()
        token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 3600))
        return token, time.time() + expires_in

    def get_management_token(self) -> str:
        """Return a valid management token, minting if cache is stale.

        File-locked to serialize concurrent mints across processes.
        """
        cache_path = self._cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(cache_path) + ".lock")
        with lock:
            cached = self._read_cached_token()
            if cached is not None:
                return cached
            token, expires_at = self._mint_management_token()
            self._write_cached_token(token, expires_at)
            return token

    # ------------------------------------------------------------------
    # HTTP layer with 429 backoff
    # ------------------------------------------------------------------

    def _request_with_backoff(
        self,
        method: str,
        url: str,
        *,
        json_data: Any = None,
        data: Any = None,
        headers: dict | None = None,
        params: dict | None = None,
        authenticated: bool = True,
    ) -> requests.Response:
        """Make a request, retrying on 429 with exponential backoff.

        Raises :class:`RateLimitError` after MAX_BACKOFF_ATTEMPTS retries.
        Non-429 responses are returned unmodified for the caller to
        interpret (success or otherwise).
        """
        request_headers = dict(headers or {})
        if authenticated:
            # Re-fetch the token each attempt — it may be cache-valid for
            # the duration of the loop, but the cache layer is cheap.
            token = self.get_management_token()
            request_headers["Authorization"] = f"Bearer {token}"

        last_resp: requests.Response | None = None
        for attempt in range(MAX_BACKOFF_ATTEMPTS):
            resp = self._session.request(
                method,
                url,
                json=json_data,
                data=data,
                headers=request_headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )
            last_resp = resp
            if resp.status_code != 429:
                return resp
            # 429 → back off then retry (unless this was the final attempt).
            if attempt < MAX_BACKOFF_ATTEMPTS - 1:
                # Prefer the server's Retry-After signal if present and
                # parseable, else fall back to formulaic exponential
                # backoff with jitter. Either way we clamp at
                # BACKOFF_MAX_SECONDS so a hostile server can't pin us
                # forever.
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                if retry_after is None:
                    retry_after = _compute_backoff(attempt)
                time.sleep(retry_after)

        assert last_resp is not None  # loop body always assigns
        raise RateLimitError(
            f"{method} {url} returned 429 after {MAX_BACKOFF_ATTEMPTS} attempts; "
            f"last body: {last_resp.text[:200]}"
        )

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        json_data: Any = None,
        params: dict | None = None,
        allow_conflict: bool = False,
        allow_missing: bool = False,
    ) -> requests.Response:
        """Authenticated Management API request with 429 backoff.

        ``allow_conflict=True`` returns 409/422 responses unmodified so
        the caller can fall back to a GET-and-find flow.

        ``allow_missing=True`` returns 404 responses unmodified so a DELETE
        of an already-absent resource is a tolerated no-op (used by teardown:
        a partially-torn-down app must not crash on the second pass).
        """
        url = f"{self.endpoint}{path}"
        resp = self._request_with_backoff(
            method,
            url,
            json_data=json_data,
            params=params,
            headers={"Content-Type": "application/json"} if json_data is not None else None,
            authenticated=True,
        )
        if resp.status_code in (409, 422) and allow_conflict:
            return resp
        if resp.status_code == 404 and allow_missing:
            return resp
        if resp.status_code >= 400:
            raise LogtoError(
                f"{method} {path} returned HTTP {resp.status_code}: {resp.text}"
            )
        return resp

    # ------------------------------------------------------------------
    # Search helpers (server-side ?search=)
    # ------------------------------------------------------------------

    def find_application(self, name: str, app_type: str | None = None) -> dict | None:
        """Return the first application matching ``name`` (and optionally ``app_type``).

        Uses server-side ``?search=`` per spec §R6 — never scans the full
        list client-side.
        """
        resp = self._api_request("GET", "/api/applications", params={"search": name})
        for app in resp.json():
            if app.get("name") == name and (app_type is None or app.get("type") == app_type):
                return app
        return None

    def find_resource(self, indicator: str) -> dict | None:
        """Return the API resource with the given indicator URL, or None."""
        resp = self._api_request("GET", "/api/resources", params={"search": indicator})
        for res in resp.json():
            if res.get("indicator") == indicator:
                return res
        return None

    def find_user(self, username: str) -> dict | None:
        """Return the user with the given username, or None."""
        resp = self._api_request("GET", "/api/users", params={"search": username})
        for user in resp.json():
            if user.get("username") == username:
                return user
        return None

    # ------------------------------------------------------------------
    # Resource creators (idempotent: check first, then create)
    # ------------------------------------------------------------------

    def create_spa_application(self, slug: str) -> dict:
        """Create (or return existing) per-app SPA application.

        Returns the full app dict including ``id`` (== client_id for SPA).

        Idempotency: if the POST races with a concurrent provisioner and
        Logto returns 409/422, we re-fetch via :meth:`find_application`
        rather than crash. Two parallel ``provision_app`` calls for the
        same slug both succeed and observe the same SPA.
        """
        name = f"{slug}-frontend"
        existing = self.find_application(name, app_type="SPA")
        if existing:
            return existing
        resp = self._api_request(
            "POST",
            "/api/applications",
            json_data={
                "name": name,
                "type": "SPA",
                "description": f"Per-app frontend SPA for {slug}",
                "oidcClientMetadata": {
                    "redirectUris": ["http://localhost:5173/callback"],
                    "postLogoutRedirectUris": ["http://localhost:5173"],
                },
            },
            allow_conflict=True,
        )
        if resp.status_code in (409, 422):
            existing = self.find_application(name, app_type="SPA")
            if existing is None:
                raise LogtoError(
                    f"POST /api/applications returned {resp.status_code} for "
                    f"{name!r} but find_application could not resolve the conflict."
                )
            return existing
        return resp.json()

    def _ensure_m2m_secret(self, app: dict) -> dict:
        """Return an adopted M2M app dict that carries a usable ``secret``.

        The M2M ``secret`` is what the orchestrator pushes into the app's
        GitHub Actions secret / Railway env var. On the ADOPT path (resume where
        STEP_LOGTO wasn't checkpointed) the app dict comes from a LIST search
        (:meth:`find_application`), and Logto list endpoints commonly OMIT the
        secret — the full secret typically only appears on the create response
        or via a detail lookup. Returning that empty secret would silently push
        an EMPTY credential downstream with no failure, so this guard fails loud:

          1. If the adopted dict already has a truthy ``secret``, use it as-is.
          2. Else re-fetch the application detail (``GET /api/applications/{id}``)
             and use its ``secret`` if present.
          3. Else raise :class:`LogtoSecretUnrecoverable` — the secret can't be
             recovered, so the caller must re-create from scratch (delete the
             bootstrap state for this app and rerun). This is a DISTINCT,
             NON-retryable exception type (Fix #15) so the orchestrator does not
             loop on it under ``--resume``.

        NOTE (plan Risk R-a family — MUST be validated against a real Logto
        instance): the exact secret-bearing field and endpoint are UNVERIFIED
        from the repo. This code assumes (a) the M2M ``secret`` lives under the
        top-level ``secret`` key and (b) ``GET /api/applications/{id}`` returns
        it. Real Logto may instead expose M2M secrets only via a dedicated
        endpoint (e.g. ``/api/applications/{id}/secrets``) and/or a different
        field name. If live validation shows that, update the detail fetch here
        accordingly — the fail-loud contract (never return an empty secret)
        stays the same.
        """
        if app.get("secret"):
            return app
        app_id = app.get("id")
        if app_id:
            detail = self._api_request("GET", f"/api/applications/{app_id}").json()
            if detail.get("secret"):
                return detail
        raise LogtoSecretUnrecoverable(
            f"M2M application {app.get('name')!r} (id={app.get('id')!r}) "
            "has no recoverable secret: the list/create result omitted it and the "
            "detail fetch did not supply one. The M2M secret cannot be recovered "
            "— delete this app's bootstrap state and rerun to re-create the M2M "
            "client from scratch. (Non-retryable: LogtoSecretUnrecoverable.)"
        )

    def create_m2m_application(self, slug: str) -> dict:
        """Create (or return existing) per-app M2M application.

        Returns the full app dict including ``id`` and ``secret``.

        Idempotency: on 409/422 from a concurrent POST, falls back to
        :meth:`find_application` to return the existing M2M app.

        Secret guard (P1 / Fix #15): EVERY return path — the adopt path (initial
        resume find OR the 409/422 conflict fallback) AND the normal create-POST
        path — is run through :meth:`_ensure_m2m_secret`, so a response that omits
        the secret can never silently return an empty credential. A create
        response lacking a usable secret fails loud (``LogtoSecretUnrecoverable``,
        a distinct NON-retryable type) rather than pushing an empty secret
        downstream.
        """
        name = f"{slug}-agent"
        existing = self.find_application(name, app_type="MachineToMachine")
        if existing:
            return self._ensure_m2m_secret(existing)
        resp = self._api_request(
            "POST",
            "/api/applications",
            json_data={
                "name": name,
                "type": "MachineToMachine",
                "description": f"Per-app M2M client for {slug} agents",
            },
            allow_conflict=True,
        )
        if resp.status_code in (409, 422):
            existing = self.find_application(name, app_type="MachineToMachine")
            if existing is None:
                raise LogtoError(
                    f"POST /api/applications returned {resp.status_code} for "
                    f"{name!r} but find_application could not resolve the conflict."
                )
            return self._ensure_m2m_secret(existing)
        # Fix #15(a): route the create-POST response through the same secret
        # guard — a create result with no usable secret must fail loud, not
        # silently return an empty credential.
        return self._ensure_m2m_secret(resp.json())

    def create_api_resource(self, slug: str, indicator: str) -> dict:
        """Create (or return existing) per-app API resource.

        Idempotency: on 409/422 from a concurrent POST, falls back to
        :meth:`find_resource` to return the existing resource.
        """
        existing = self.find_resource(indicator)
        if existing:
            return existing
        resp = self._api_request(
            "POST",
            "/api/resources",
            json_data={
                "name": f"{slug} API",
                "indicator": indicator,
            },
            allow_conflict=True,
        )
        if resp.status_code in (409, 422):
            existing = self.find_resource(indicator)
            if existing is None:
                raise LogtoError(
                    f"POST /api/resources returned {resp.status_code} for "
                    f"{indicator!r} but find_resource could not resolve the conflict."
                )
            return existing
        return resp.json()

    def create_scopes(self, resource_id: str, slug: str) -> list[dict]:
        """Create the default ``read`` + ``write`` scopes on the resource."""
        out: list[dict] = []
        for scope_def in (
            {"name": name, "description": f"{name.capitalize()} access to {slug} API"}
            for name in DEFAULT_SCOPES
        ):
            resp = self._api_request(
                "POST",
                f"/api/resources/{resource_id}/scopes",
                json_data=scope_def,
                allow_conflict=True,
            )
            if resp.status_code in (409, 422):
                # Already exists — fetch the full scope list and find ours.
                list_resp = self._api_request(
                    "GET", f"/api/resources/{resource_id}/scopes"
                )
                for s in list_resp.json():
                    if s.get("name") == scope_def["name"]:
                        out.append(s)
                        break
            else:
                out.append(resp.json())
        return out

    # ------------------------------------------------------------------
    # Roles (U-roles). Scopes created by :meth:`create_scopes` exist on the
    # resource but are not granted to anybody until a role carries them and
    # something is assigned that role. Without this step every minted token —
    # user AND M2M — comes back with an empty `scope` claim, and a middleware
    # that enforces a required scope 403s every single request.
    # ------------------------------------------------------------------

    def find_role(self, name: str) -> dict | None:
        """Return the role with the given name, or None."""
        resp = self._api_request("GET", "/api/roles")
        for role in resp.json():
            if role.get("name") == name:
                return role
        return None

    def create_role(
        self, name: str, role_type: str, description: str, scope_ids: list[str]
    ) -> dict:
        """Create (or return existing) a role carrying ``scope_ids``.

        ``role_type`` is Logto's discriminator: ``"User"`` roles can only be
        assigned to users, ``"MachineToMachine"`` roles only to applications.
        They are separate namespaces, so an app needs one of each.

        Idempotency: on 409/422, falls back to :meth:`find_role`. If the role
        already existed, any missing scopes are attached rather than assumed —
        a role created by an earlier partial run may carry none.
        """
        existing = self.find_role(name)
        if existing is None:
            resp = self._api_request(
                "POST",
                "/api/roles",
                json_data={
                    "name": name,
                    "description": description,
                    "type": role_type,
                    "scopeIds": scope_ids,
                },
                allow_conflict=True,
            )
            if resp.status_code in (409, 422):
                existing = self.find_role(name)
                if existing is None:
                    raise LogtoError(
                        f"POST /api/roles returned {resp.status_code} for "
                        f"{name!r} but find_role could not resolve the conflict."
                    )
            else:
                return resp.json()

        self.attach_scopes_to_role(existing["id"], scope_ids)
        return existing

    def attach_scopes_to_role(self, role_id: str, scope_ids: list[str]) -> None:
        """Attach any of ``scope_ids`` the role does not already carry."""
        if not scope_ids:
            return
        current = self._api_request("GET", f"/api/roles/{role_id}/scopes")
        have = {s.get("id") for s in current.json()}
        missing = [sid for sid in scope_ids if sid not in have]
        if not missing:
            return
        self._api_request(
            "POST",
            f"/api/roles/{role_id}/scopes",
            json_data={"scopeIds": missing},
            allow_conflict=True,
        )

    def assign_role_to_user(self, role_id: str, user_id: str) -> None:
        """Assign a User-type role to a user. Idempotent."""
        current = self._api_request("GET", f"/api/roles/{role_id}/users")
        if any(u.get("id") == user_id for u in current.json()):
            return
        self._api_request(
            "POST",
            f"/api/roles/{role_id}/users",
            json_data={"userIds": [user_id]},
            allow_conflict=True,
        )

    def assign_role_to_application(self, role_id: str, app_id: str) -> None:
        """Assign a MachineToMachine-type role to an application. Idempotent."""
        current = self._api_request("GET", f"/api/roles/{role_id}/applications")
        if any(a.get("id") == app_id for a in current.json()):
            return
        self._api_request(
            "POST",
            f"/api/roles/{role_id}/applications",
            json_data={"applicationIds": [app_id]},
            allow_conflict=True,
        )

    def update_application_redirect_uris(
        self, app_id: str, new_uris: list[str]
    ) -> dict[str, Any]:
        """Merge ``new_uris`` into an SPA app's OIDC redirect URIs (U6 / SC3).

        CRITICAL merge semantics (plan Risk R-d): GET the app's current
        ``oidcClientMetadata.redirectUris``, UNION in ``new_uris`` (preserving
        order and every existing entry — notably the localhost dev URIs the
        SPA was created with), then PATCH the merged list back. This is NEVER a
        replace: registering the prod redirect URI must not clobber local dev
        sign-in.

        Idempotent: if every URI in ``new_uris`` is already present, no PATCH
        is issued and the current list is returned with ``patched=False`` — so
        a ``--resume`` re-running this step is a harmless GET, not a duplicate
        write.

        The Logto Management API field is ``oidcClientMetadata.redirectUris`` —
        confirmed against :meth:`create_spa_application`, which sets exactly
        that path when creating the SPA. The whole ``oidcClientMetadata`` object
        is copied and PATCHed back (with only ``redirectUris`` changed) so
        sibling keys like ``postLogoutRedirectUris`` survive the update.

        Returns
        -------
        dict
            ``{"redirect_uris": <post-state list>, "patched": <bool>}``. The
            post-state list is read from an authoritative follow-up
            ``GET /api/applications/{id}`` (Fix #16), NOT the PATCH echo — so a
            silent no-op (a PATCH that returned 2xx but didn't persist) is caught
            HERE: if the re-fetched list omits any intended URI, this raises
            :class:`LogtoError` rather than falling back to the requested list
            and declaring a broken prod sign-in a success.
        """
        resp = self._api_request("GET", f"/api/applications/{app_id}")
        app = resp.json()
        metadata = dict(app.get("oidcClientMetadata") or {})
        current = list(metadata.get("redirectUris") or [])

        merged = list(current)
        for uri in new_uris:
            if uri not in merged:
                merged.append(uri)

        if merged == current:
            # Every requested URI already present — idempotent no-op.
            return {"redirect_uris": current, "patched": False}

        metadata["redirectUris"] = merged
        self._api_request(
            "PATCH",
            f"/api/applications/{app_id}",
            json_data={"oidcClientMetadata": metadata},
        )

        # Fix #16: verify persistence AUTHORITATIVELY. A 2xx PATCH is not proof
        # the write landed — the response may omit or empty ``redirectUris``.
        # Do NOT trust the PATCH echo and NEVER fall back to the requested list
        # (that would let a silent no-op pass the caller's assert-after-write).
        # Re-fetch and confirm every intended URI is present in the RETURNED
        # list; if not, raise so a broken prod sign-in surfaces loudly.
        #
        # Risk R-a: the exact metadata field (``oidcClientMetadata.redirectUris``)
        # is ASSUMED — confirmed against create_spa_application, which sets that
        # path — and needs live validation against a real Logto instance.
        verify_resp = self._api_request("GET", f"/api/applications/{app_id}")
        verify_metadata = verify_resp.json().get("oidcClientMetadata") or {}
        persisted_uris = list(verify_metadata.get("redirectUris") or [])
        missing = [uri for uri in new_uris if uri not in persisted_uris]
        if missing:
            raise LogtoError(
                f"redirect URI update for application {app_id!r} did not persist: "
                f"{missing!r} absent from the re-fetched redirectUris "
                f"{persisted_uris!r}. Refusing to report success on an "
                "unconfirmed write (prod sign-in would silently break)."
            )
        return {"redirect_uris": persisted_uris, "patched": True}

    def create_user(self, slug: str) -> dict:
        """Create (or return existing) per-app user with username ``app_<slug>``.

        The slug's hyphens are converted to underscores: Logto usernames must
        match ``^[A-Za-z_]\\w*$`` (letters/underscore start, then word chars),
        so a hyphenated slug like ``my-sample-notes-app`` would be rejected
        (``guard.invalid_input`` on ``username``). Underscores are valid.

        Idempotency: on 409/422 from a concurrent POST, falls back to
        :meth:`find_user` to return the existing user.
        """
        username = LOGTO_USER_PREFIX + slug.replace("-", "_")
        existing = self.find_user(username)
        if existing:
            return existing
        resp = self._api_request(
            "POST",
            "/api/users",
            json_data={
                "username": username,
                "name": f"App {slug}",
            },
            allow_conflict=True,
        )
        if resp.status_code in (409, 422):
            existing = self.find_user(username)
            if existing is None:
                raise LogtoError(
                    f"POST /api/users returned {resp.status_code} for "
                    f"{username!r} but find_user could not resolve the conflict."
                )
            return existing
        return resp.json()

    # ------------------------------------------------------------------
    # Resource deleters (U8 / teardown) — DELETE the per-app resources a
    # bootstrap run created. Each tolerates a 404 (already-absent) so a
    # partial teardown never crashes; any other >=400 raises LogtoError.
    # ------------------------------------------------------------------

    def delete_application(self, app_id: str) -> bool:
        """DELETE a per-app application (SPA or M2M) by its id.

        Returns ``True`` if the resource was present and deleted, ``False``
        if it was already absent (404). Never deletes anything the caller
        didn't name — the shared ``agentic-core-admin`` guard lives in the
        teardown orchestration (:func:`agentic_core.bootstrap.teardown`).
        """
        resp = self._api_request(
            "DELETE", f"/api/applications/{app_id}", allow_missing=True
        )
        return resp.status_code != 404

    def delete_api_resource(self, resource_id: str) -> bool:
        """DELETE a per-app API resource by its INTERNAL id.

        Note the state file records the resource *indicator* URL, not this id
        — the teardown orchestration resolves the id via :meth:`find_resource`
        before calling this.
        """
        resp = self._api_request(
            "DELETE", f"/api/resources/{resource_id}", allow_missing=True
        )
        return resp.status_code != 404

    def delete_user(self, user_id: str) -> bool:
        """DELETE a per-app user by its INTERNAL id.

        The state file records the *username* (``app_<slug>``); the
        teardown orchestration resolves the id via :meth:`find_user` first.
        """
        resp = self._api_request(
            "DELETE", f"/api/users/{user_id}", allow_missing=True
        )
        return resp.status_code != 404


# ---------------------------------------------------------------------------
# Public entry point: provision_app
# ---------------------------------------------------------------------------


def provision_app(
    *,
    slug: str,
    endpoint: str,
    admin_app_id: str,
    admin_credential: bytearray,
    platform_domain: str,
    cache_path: Path | None = None,
    with_spa: bool = True,
    with_m2m: bool = True,
    with_api_resource: bool = True,
    with_user: bool = True,
    persist: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Provision a per-app Logto footprint, gated by R10 variant flags.

    Spec §R10 variant×invariant matrix — the four ``with_*`` booleans map
    1:1 to the matrix columns and let the orchestrator (task #8) ask for
    exactly the resources a given variant needs:

      ===========================  ===  ===  ===  ===
      Variant                       SPA  M2M  API  Usr
      ===========================  ===  ===  ===  ===
      ``full``                      Y    Y    Y    Y
      ``lightweight-api``           N    Y    Y    Y
      ``static-dashboard --auth``   Y    N    N    Y
      ``static-dashboard --no-auth`` -- (orchestrator skips this call entirely)
      ===========================  ===  ===  ===  ===

    Defaults preserve the original task-#4 behavior (all True == ``full``)
    so the existing ``test_bootstrap_logto.py`` continues to pass without
    edits. Skipped resources do NOT POST and DO NOT appear (or appear as
    None / empty) in the return dict.

    Two consecutive calls for two different slugs (any combination of
    flags) always return fully distinct identifiers for the resources
    they did create.

    Args:
      slug: Lowercase app slug (validated upstream in the CLI).
      endpoint: Logto base URL.
      admin_app_id: Bootstrap admin M2M application ID (the
        ``agentic-core-admin`` client, NOT the per-app M2M created here).
      admin_credential: Bootstrap admin M2M secret as a ``bytearray``.
        The keystore caller owns lifecycle.
      platform_domain: Domain for the API resource URL. The resource
        indicator will be ``https://<slug>.<platform_domain>/api``. There is
        no default: each operator names their own domain.
      cache_path: Override for the management token cache file.
      with_spa: Create the per-app SPA application. Default True.
      with_m2m: Create the per-app M2M application. Default True.
      with_api_resource: Create the per-app API resource and its
        ``read``/``write`` scopes. Default True. Required if ``with_m2m``
        is True (the M2M needs an audience to target).
      with_user: Create the per-app ``app_<slug>`` user. Default True.
      persist: Optional callback ``(state_key, value) -> None`` invoked the
        moment each resource is created (or adopted via its finder on
        resume), so the orchestrator can write the resource ID into the
        bootstrap state file incrementally rather than in one batch after
        this function returns (§R2 / KTD3). A mid-sequence failure therefore
        leaves already-created resources recorded — not orphaned. The
        callback receives ONLY non-secret IDs/URLs under the ``logto_*``
        state keys (``logto_spa_client_id``, ``logto_m2m_app_id``,
        ``logto_api_resource``, ``logto_user_sub``); the M2M *secret* is
        never handed to it (state entropy gate forbids secrets). ``None``
        (default) makes provisioning behave exactly as before.

    Returns:
      A dict with the keys ``spa_client_id``, ``m2m_app_id``,
      ``m2m_app_secret``, ``api_resource``, ``user_sub``, ``endpoint``,
      ``scopes``. Skipped resources appear as ``None`` (single values) or
      ``[]`` (``scopes``). Shape matches the ``logto`` block of the spec
      §A2 JSON contract.

    Raises:
      RateLimitError: After exhausting backoff retries on 429.
      LogtoAuthError: If the admin credential is rejected.
      LogtoError: For other Management API failures.
    """
    client = LogtoManagementClient(
        endpoint=endpoint,
        admin_app_id=admin_app_id,
        admin_credential=admin_credential,
        cache_path=cache_path,
    )

    indicator = api_resource_indicator(slug, platform_domain)

    def _persist(key: str, value: Any) -> None:
        # Only non-secret IDs/URLs reach here (never the M2M secret). Guard
        # on a truthy value so a missing id doesn't write an empty string.
        if persist is not None and value:
            persist(key, str(value))

    spa = client.create_spa_application(slug) if with_spa else None
    if spa:
        _persist("logto_spa_client_id", spa.get("id"))

    m2m = client.create_m2m_application(slug) if with_m2m else None
    if m2m:
        # Persist the M2M app *id* only — the secret (m2m["secret"]) stays
        # in-memory and is returned to the caller, never handed to persist.
        _persist("logto_m2m_app_id", m2m.get("id"))

    resource: dict | None
    scopes_list: list[dict]
    if with_api_resource:
        resource = client.create_api_resource(slug, indicator)
        # The state records the indicator URL (matches the return dict), not
        # the internal resource id, keeping parity with the old batch write.
        _persist("logto_api_resource", indicator)
        scopes_list = client.create_scopes(resource["id"], slug)
    else:
        resource = None
        scopes_list = []

    user = client.create_user(slug) if with_user else None
    # Fallback username mirrors create_user's hyphen->underscore sanitization
    # (Logto usernames must match ^[A-Za-z_]\w*$).
    _fallback_username = LOGTO_USER_PREFIX + slug.replace("-", "_")
    if user:
        # NOTE: this key holds the USERNAME because teardown resolves the user
        # by username to delete it. The actor-sub identity is the user *id* —
        # see logto_user_id below.
        _persist("logto_user_sub", user.get("username", _fallback_username))
        _persist("logto_user_id", user.get("id"))

    # ------------------------------------------------------------------
    # Roles. create_scopes() put `read`/`write` on the resource, but a scope
    # nobody holds is never emitted in a token. Grant them via one role per
    # principal kind — Logto keeps User and MachineToMachine roles in separate
    # namespaces, so an app that serves both humans and agents needs both.
    #
    # Skipped silently before this was added, which is why every generated app
    # minted scope-less tokens and 403'd on the middleware's required-scope
    # check. See spec R9.
    # ------------------------------------------------------------------
    scope_ids = [sc["id"] for sc in scopes_list if sc.get("id")]
    user_role_name, agent_role_name = role_names(slug)
    user_role = None
    agent_role = None
    if scope_ids and user is not None:
        user_role = client.create_role(
            user_role_name,
            "User",
            f"Human users of the {slug} API",
            scope_ids,
        )
        _persist("logto_user_role_id", user_role.get("id"))
        client.assign_role_to_user(user_role["id"], user["id"])
    if scope_ids and m2m is not None:
        agent_role = client.create_role(
            agent_role_name,
            "MachineToMachine",
            f"Agents and CLI callers of the {slug} API",
            scope_ids,
        )
        _persist("logto_agent_role_id", agent_role.get("id"))
        client.assign_role_to_application(agent_role["id"], m2m["id"])

    return {
        "spa_client_id": spa["id"] if spa else None,
        "m2m_app_id": m2m["id"] if m2m else None,
        "m2m_app_secret": (m2m.get("secret", "") if m2m else None),
        "api_resource": indicator if resource is not None else None,
        "user_sub": (
            user.get("username", _fallback_username) if user else None
        ),
        # The value the CLI must send as X-{App}-Actor-Sub, and the value a
        # browser login puts in `sub`. These MUST be the same string or the
        # same human ends up with two UserProfile rows.
        "user_id": user.get("id") if user else None,
        "user_role_id": user_role.get("id") if user_role else None,
        "agent_role_id": agent_role.get("id") if agent_role else None,
        "endpoint": endpoint.rstrip("/"),
        "scopes": [s["name"] for s in scopes_list],
    }


def update_app_redirect_uris(
    *,
    endpoint: str,
    admin_app_id: str,
    spa_app_id: str,
    prod_redirect_uri: str,
    cache_path: Path | None = None,
    admin_credential: bytearray | None = None,
) -> dict[str, Any]:
    """Merge ``prod_redirect_uri`` into an SPA app's OIDC redirect URIs (U6).

    Module-level wrapper mirroring :func:`provision_app`: the orchestrator's
    STEP_WIRING calls this rather than constructing a client itself.

    Credential note: STEP_WIRING runs *after* the orchestrator has already
    zeroed the admin credential following Logto provisioning (PERF-003 — the
    secret lives for the minimum window). This call therefore relies on the
    on-disk **management-token cache** the Logto client already maintains: the
    STEP_LOGTO provisioning that ran seconds earlier in the same bootstrap
    warmed it, and the token's ~1h TTL keeps it valid across a quick
    ``--resume`` in a fresh process. ``admin_credential`` defaults to an empty
    ``bytearray`` (cache-only mode); if a caller still holds a live credential
    it may pass it so a cold cache can re-mint. When neither the cache nor a
    credential can produce a token, the underlying request raises
    :class:`LogtoAuthError` / :class:`LogtoError`, which the orchestrator routes
    to a *retryable* failure (re-run once auth is fresh).

    Args:
      endpoint: Logto base URL.
      admin_app_id: Bootstrap admin M2M application ID (mint identity).
      spa_app_id: The per-app SPA application id whose redirect URIs to update.
      prod_redirect_uri: The prod ``https://<domain>/callback`` to merge in.
      cache_path: Override for the management token cache file.
      admin_credential: Optional live admin secret; defaults to cache-only.

    Returns:
      ``{"redirect_uris": <post-state list>, "patched": <bool>}`` — see
      :meth:`LogtoManagementClient.update_application_redirect_uris`.
    """
    client = LogtoManagementClient(
        endpoint=endpoint,
        admin_app_id=admin_app_id,
        admin_credential=admin_credential if admin_credential is not None else bytearray(),
        cache_path=cache_path,
    )
    return client.update_application_redirect_uris(spa_app_id, [prod_redirect_uri])
