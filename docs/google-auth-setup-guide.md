# Google Sign-In for New Apps

New apps use Google sign-in by default. Choose Logto with `--auth-provider logto`.

## Which apps get which provider

| How the app is made | `auth_provider` | Sign-in |
|---|---|---|
| `agentic-core create-app <name>` (Render, the default) | `google` | Google sign-in, agent keys for the CLI and MCP server |
| `agentic-core create-app <name> --deploy-target railway` | `google` | Same sign-in, with manual settings on Railway |
| `agentic-core create-app <name> --auth-provider logto` | `logto` | Manual setup on Render. Add `--deploy-target railway` for automated setup. |
| `copier update` on an app made before this change | `logto` | Unchanged. The Copier default stays `logto`. |

The provider flags apply to new apps. They do not migrate a deployed app.

## Design

![How Google sign-in works: the browser asks the app backend to sign in, the backend sends the browser to Google, Google sends it back with a code, the backend trades the code for an ID token, checks the token and the email allowlist, and sets a session cookie.](diagrams/google-sign-in-sequence.svg)

For each step in Google Cloud Console, see the [Google sign-in walkthrough](google-oauth-walkthrough.md).

- **Library.** The backend uses Authlib's Starlette client for the authorization code flow. Authlib makes the `state`, the `nonce`, and the PKCE verifier and checks the ID token signature, issuer, expiry, and nonce.
- **Audience.** The backend passes the client ID to Authlib's `aud` check. Authlib accepts any `aud` list that contains it. The backend then also requires `aud` to be exactly the app's `GOOGLE_CLIENT_ID`, or a one-item list of it.
- **Scopes.** Only `openid email`.
- **Session.** A cookie signed with `SESSION_SECRET`. It is HttpOnly and SameSite=Lax. On https it is Secure and named `__Host-session`. It holds the user ID (`google:<sub>`), the email, the sign-in time, and a CSRF token. It holds no Google token. The backend drops the Google tokens after the callback.
- **Authorization.** Only emails in `AUTH_ALLOWED_EMAILS` get access. The backend checks the list at sign-in and on every request. An empty list gives access to nobody. A new Google account gets no access by signing up.
- **CSRF.** Browser writes must send the session's CSRF token in `X-CSRF-Token`.
- **Redirects.** The redirect URI is `<APP_BASE_URL>/auth/callback`, built from configuration and never from the request `Host` header. After sign-in the browser returns only to a path on the same app.
- **Fail closed.** While a required setting is missing or invalid, every protected route and every `/auth/*` route return 503 `auth_not_configured`. `/health` returns 200. The required settings are `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SESSION_SECRET` (at least 32 characters), `AUTH_ALLOWED_EMAILS` (at least one email), and a valid base URL (`APP_BASE_URL`, else `RENDER_EXTERNAL_URL`).
- **Frontend.** The web app asks `GET /auth/session` who is signed in. It stores no sign-in data in `localStorage` or `sessionStorage`. The only stored value is the light or dark theme choice, under the `localStorage` key `theme`.

## Machine access: agent keys

Google sign-in is for people in a browser. The API never accepts a Google ID token, a Google access token, or a Logto token as a machine credential. Agents use an agent key:

- The operator creates the key with `python -m app.auth.agent_keys new`. The tool writes the key to a new 0600 file and prints only the file path and the public entry.
- The server keeps only the SHA-256 hash of the secret, in `AGENT_CREDENTIALS`.
- Each key acts for exactly one person (`google:<sub>`). The request must name that person in the `X-<APP>-Actor-Sub` header.
- A key reads only, unless the operator allows writes with `--write`.
- A key stops at its optional `expires` instant (UTC), or when its entry is removed.
- A malformed, duplicate, or unknown entry rejects every machine request with 503 `agent_credentials_invalid`. Browser sign-in still works.

The per-app CLI and MCP server both accept these keys. See [the agent key contract](agent-auth-contract.md). The generated app's `README.md`, section "Machine credential", gives their settings.

## Operator setup

The generated app's `README.md` has the full steps in the section "Google sign-in": the Render setup order, the Google Cloud Console steps, local setup, agent keys, and a table of every failure response. In short:

1. Apply the Render Blueprint with the Google values empty. The app returns 503.
2. Copy the service URL from the Render dashboard.
3. In Google Cloud Console, set the consent screen audience to **External**. Publish it to **In production**, or add each person as a test user.
4. Create an OAuth client of type **Web application** with the redirect URI `<service URL>/auth/callback`.
5. In the Render dashboard, set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `AUTH_ALLOWED_EMAILS`. Render generates `SESSION_SECRET`.

On Railway, use the app service URL instead of the Render URL. Set the same Google settings in the service variables. Also set `APP_BASE_URL` to that URL and `SESSION_SECRET` to at least 32 random characters.

Use one Google OAuth client per app. A shared client would let one app's ID tokens pass another app's audience check.

The `create-app --json` result for a Google app with a backend has an `auth` block: `provider` (`google_oidc`), `status` (`not_configured`), `required_settings` (names only), `limitations`, and `next_step`.

## Not supported yet

- A mobile app with Google sign-in. A Google-mode app has no `mobile/` folder, and Copier rejects `include_mobile=true` with `auth_provider=google`.
- A Google Workspace domain allowlist (the `hd` claim).
- Revoking one session. Removing the email from the allowlist ends that person's browser access at the next request after the service restarts. Rotating `SESSION_SECRET` ends all sessions.
- Revoking agent keys through the allowlist. An agent key has no email. To end a person's access, also remove every `AGENT_CREDENTIALS` entry whose `actor` is that person.

## Tests

- Each generated Google app runs `tests/integration/test_google_auth.py` and `tests/integration/test_agent_keys.py`. A fake Google provider (`tests/google_fake.py`) serves discovery, JWKS, and token responses through `httpx.MockTransport`, so no test reaches the network.
- In this repo, `tests/unit/test_template_google_auth.py` checks what each provider renders, and `tests/integration/test_generated_google_app.py` installs a generated Google app and runs its suite and `ruff`.
