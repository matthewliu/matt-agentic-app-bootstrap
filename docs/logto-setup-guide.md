# Logto Setup Guide

> **Option guide.** Logto is a supported sign-in option. The default is Google sign-in: see [google-oauth-walkthrough.md](google-oauth-walkthrough.md). Use this guide for an app made with `--auth-provider logto`. Read [choosing-options.md](choosing-options.md) first: you must host your own Logto. The README section "Other options: Logto and Railway" lists the setup steps.

> How to integrate a new mini-app with the shared Logto auth provider.
> Last verified: 2026-03-10 | Logto v1.37.1

## Logto Instance Info

Replace each `<your-logto-...>` value with the URLs of your own Logto deployment.

| Item | URL |
|------|-----|
| Auth Endpoint | `https://<your-logto-auth>.up.railway.app` |
| Admin Console | `https://<your-logto-admin-console>.up.railway.app` |
| OIDC Discovery | `https://<your-logto-auth>.up.railway.app/oidc/.well-known/openid-configuration` |
| JWKS URI | `https://<your-logto-auth>.up.railway.app/oidc/jwks` |
| Token Endpoint | `https://<your-logto-auth>.up.railway.app/oidc/token` |
| Issuer (`iss` claim) | `https://<your-logto-auth>.up.railway.app/oidc` |

**Important:** The Railway default domain is baked into the OIDC issuer (`iss`) claim of all tokens. Changing this domain later requires reissuing all M2M credentials and updating every app's `LOGTO_ENDPOINT`.

## How to Register a New App

### Traditional Web App (Human SSO)

1. Open the [Admin Console](https://<your-logto-admin-console>.up.railway.app)
2. Go to **Applications** → **Create Application**
3. Select **Traditional Web**
4. Name it descriptively (e.g., `Habit Tracker Web`)
5. Configure redirect URIs:
   - Dev: `http://localhost:<port>/callback`
   - Prod: `https://<app-domain>/callback`
6. Save and note the **Client ID** and **Client Secret**
7. Store credentials as Railway env vars in the app's Railway project (never in code)

### Machine-to-Machine App (Agent Auth)

1. Open the [Admin Console](https://<your-logto-admin-console>.up.railway.app)
2. Go to **Applications** → **Create Application**
3. Select **Machine-to-Machine**
4. Name it descriptively (e.g., `Habit Tracker Agent`)
5. Save and note the **Client ID** and **Client Secret**
6. Create or select a **Role** (see below) and assign it to this app
7. Store credentials as Railway env vars (never in code)

## How to Create an API Resource

Each app should have its own API Resource representing its API surface.

1. Go to **API Resources** → **Create API Resource**
2. Set the **indicator** (a URI identifier — does not need to resolve):
   - Convention: `https://<app-name>.<your-domain>/api` (the shape `agentic_core/bootstrap/logto.py` builds; an `api.<app-name>` form fails every audience check)
   - Example: `https://habit-tracker.example.dev/api`
3. Add **scopes** (permissions):
   - The bootstrap creates `read` and `write`. The generated middleware requires `read`.
   - Start minimal — add scopes as needed

### Roles

1. Go to **Roles** → **Create Role**
2. Select **Machine-to-Machine app role**
3. Name it: `<app-name>-agent` (e.g., `habit-tracker-agent`)
4. Assign the scopes the agent needs from the API Resource
5. Assign the role to the M2M application

## Environment Variables for Apps

Every app needs these env vars to integrate with Logto:

| Variable | Description | Example |
|----------|-------------|---------|
| `LOGTO_ENDPOINT` | Logto auth endpoint (no trailing slash) | `https://<your-logto-auth>.up.railway.app` |
| `LOGTO_APP_ID` | Application's client_id from Logto | `abc123def456` |
| `LOGTO_APP_SECRET` | Application's client_secret from Logto | `secret-value` |
| `LOGTO_API_RESOURCE` | API Resource indicator URL | `https://habit-tracker.example.dev/api` |

**Where to store:**
- **Development:** `scripts/verify-logto/.env` (gitignored) or app-level `.env`
- **Production:** Railway environment variables in the app's Railway project
- **Never:** Committed to any repository

## JWT Validation Middleware Pattern

Copy this into your FastAPI app. It validates JWTs issued by Logto for M2M and user auth.

```python
"""JWT validation middleware for Logto integration (~30 lines of core logic)."""

import os
import ssl
import certifi
import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

LOGTO_ENDPOINT = os.environ["LOGTO_ENDPOINT"]
LOGTO_API_RESOURCE = os.environ["LOGTO_API_RESOURCE"]
JWKS_URL = f"{LOGTO_ENDPOINT}/oidc/jwks"
ISSUER = f"{LOGTO_ENDPOINT}/oidc"

# Cache JWKS keys (PyJWKClient caches for 300s by default, refreshes on kid miss)
_ssl_ctx = ssl.create_default_context(cafile=certifi.where())
_jwk_client = PyJWKClient(JWKS_URL, cache_jwk_set=True, lifespan=3600, ssl_context=_ssl_ctx)

security = HTTPBearer()


async def validate_token(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> dict:
    """Validate a Bearer JWT token. Returns decoded claims on success."""
    token = credentials.credentials
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES384"],
            audience=LOGTO_API_RESOURCE,
            issuer=ISSUER,
        )
        return claims
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Invalid audience")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="Invalid issuer")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def require_scope(required: str):
    """Dependency factory that enforces a specific scope on the token."""
    async def _check(claims: dict = Depends(validate_token)) -> dict:
        scopes = claims.get("scope", "").split()
        if required not in scopes:
            raise HTTPException(status_code=403, detail="Insufficient scope")
        return claims
    return _check


# Usage in a route:
# from fastapi import Depends
#
# @app.get("/api/resource")
# async def get_resource(claims: dict = Depends(require_scope("read:data"))):
#     user_or_app_id = claims["sub"]
#     ...
```

### JWKS Caching Strategy

- **Default TTL:** 3600 seconds (1 hour) via `lifespan` parameter
- **Key rotation:** PyJWKClient auto-refreshes on `kid` miss (new key published → next request fetches updated JWKS)
- **Fallback:** If Logto is unreachable and cache is stale, token validation fails with 401. Apps should handle gracefully (retry with backoff).
- **HTTP timeout:** PyJWKClient uses urllib with a 30s default timeout

### Claims to Validate

| Claim | What It Is | Validation |
|-------|-----------|------------|
| `iss` | Issuer | Must equal `{LOGTO_ENDPOINT}/oidc` |
| `aud` | Audience | Must equal your app's API Resource indicator |
| `exp` | Expiry | Must be in the future (PyJWT checks automatically) |
| `sub` | Subject | User ID (human) or M2M app ID (agent) |
| `scope` | Scopes | Check for required scopes per endpoint |
| `client_id` | Client ID | Identifies the calling application |

## Troubleshooting

### Common Errors

| Error | HTTP Status | Cause | Fix |
|-------|-------------|-------|-----|
| `invalid_client` | 401 | Wrong client_id or client_secret | Verify credentials in Logto admin console → Applications |
| `invalid_target` | 400 | API Resource indicator not found | Check the indicator URL in Logto admin → API Resources |
| `invalid_scope` | 400 | Requested scope not assigned to the app's role | Check Roles → assigned scopes match what's requested |
| `access_denied` | 403 | M2M app not assigned a role for that resource | Assign a role with the needed scopes to the M2M app |
| SSL certificate error | N/A | macOS Python missing CA certs | Use `certifi` package: `ssl.create_default_context(cafile=certifi.where())` |

### Where to Find Logs

- **Railway service logs:** Railway dashboard → `logto-auth` project → Logto service → Deployments → Logs
- **Logto audit logs:** Admin Console → Audit Logs (shows sign-in attempts, token grants, admin actions)
- **App-side errors:** Check your app's logs for JWT validation failures

### Verification Scripts

Run the verification scripts to confirm Logto is working:

```bash
cd scripts/verify-logto
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Fill in real credentials
bash run_all.sh
```

Expected: 9/9 tests pass covering M2M token acquisition, JWKS validation, and token claims.
