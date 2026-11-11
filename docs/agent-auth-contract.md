# Machine Auth Contract for CLI and MCP

Agreed on 2026-11-10 between the CLI and MCP lead (branch `feat/fastapi-cli-mcp`) and the Google auth lead (branch `feat/google-auth-defaults`). This file is the single record. A chat message does not change it. A change needs an edit to this file that both leads accept.

## Scope

This contract covers how the generated per-app CLI and MCP server authenticate to the generated app's own HTTP API.

Google sign-in is for people in a browser only. The API never accepts a Google ID token or a Google access token as a machine credential.

## Template answer

- Copier answer: `auth_provider`. Values: `google` or `logto`. Default: `logto`.
- `create-app` passes `google` for the Render target and `logto` for the Railway target.
- `copier update` on an old app keeps `logto`, so its auth does not change.

## Logto mode (`auth_provider == logto`)

No change from the current template.

- The client gets an access token from Logto with the OAuth 2.0 client credentials grant.
- Client env: `<APP>_API_URL`, `<APP>_LOGTO_ENDPOINT`, `<APP>_API_RESOURCE`, `<APP>_M2M_APP_ID`, `<APP>_M2M_APP_SECRET`, `<APP>_USER_SUB`, optional `<APP>_M2M_SCOPES`.
- Request headers: `Authorization: Bearer <access token>` and `X-<APP>-Actor-Sub: <actor>`.
- The server checks the actor against `<APP>_M2M_ACTOR_SUBS`.

## Google mode (`auth_provider == google`): agent key

The machine credential is an agent key that the server configures for one app.

### Credential

- Format: `agk.<key_id>.<secret>`.
- `key_id` matches `^[a-z0-9][a-z0-9-]{0,31}$`.
- `secret` is 32 random bytes in URL-safe base64 with no padding: 43 characters, no dot.
- The key is bound to one actor. The actor is the human's user ID, `google:<google sub>`.
- The operator gets the actor value from `GET /auth/session` (field `user_id`) after the person signs in once with Google.

### Client (owned by the CLI and MCP lead)

- Env: `<APP>_API_URL`, `<APP>_USER_SUB`, and one key source.
- Preferred key source: `<APP>_AGENT_KEY_FILE`, the path of a key file. The client reads the file and strips one trailing newline. The key then never passes through a shell or a model context.
- Fallback key source: `<APP>_AGENT_KEY`, the key itself. The client uses it only when `<APP>_AGENT_KEY_FILE` is not set.
- When `<APP>_AGENT_KEY_FILE` is set, the client never falls back to `<APP>_AGENT_KEY`. A missing file, an unreadable file, a file that group or other users can read, or bad content gives exit code 5.
- Request headers: `Authorization: Bearer agk.<key_id>.<secret>` and `X-<APP>-Actor-Sub: <APP>_USER_SUB`.
- The client checks the key format and the presence of `<APP>_USER_SUB` before any network call. A missing or bad value gives exit code 5 and error code `misconfigured`.
- The client never writes the key to output, logs, or error messages.
- `get_token()` returns the key. `invalidate()` does nothing. There is no token exchange.

### Server (owned by the Google auth lead)

- Env: `AGENT_CREDENTIALS`, a JSON list of entries: `{"id": "<key_id>", "sha256": "<64 hex chars of sha256(secret)>", "actor": "google:<sub>", "scopes": ["read"] or ["read", "write"], "expires": "YYYY-MM-DDTHH:MM:SSZ"}`. `expires` is optional. It is an exact UTC instant, only in this form, and the `Z` is required. The key is valid while now is before `expires`. At or after that instant the server returns 401 `agent_credential_expired`.
- These make the whole setting invalid: a duplicate `id`, a duplicate `sha256`, an unknown field, a bad `id`, `sha256`, `actor`, `scopes`, or `expires` value, or a value that is not a list. The server then returns 503 `agent_credentials_invalid` for every Bearer request. Browser sign-in still works.
- The server stores only hashes. It compares with `hmac.compare_digest`. Logs hold the key ID, the actor, and the decision. They never hold the key.
- When an `Authorization` header is present, that header alone decides. The session cookie is ignored on that request. A failure never falls back to the cookie.
- Agent-key requests skip the CSRF check.
- Scope `read` allows GET, HEAD, and OPTIONS. POST, PUT, PATCH, and DELETE need `write`. The default is read only.
- Success claims: `{"sub": "<actor>", "auth_method": "agent_key", "agent_key_id": "<key_id>", "scopes": [...]}`. Data lands under the same user ID as the person's browser session.
- Operator tool: `python -m app.auth.agent_keys new --id <id> --actor google:<sub> --out <key-file> [--write] [--expires YYYY-MM-DDTHH:MM:SSZ]`.
- The tool creates `<key-file>` with exclusive create and mode 0600. It refuses a path that exists and a symlink.
- The tool never prints the key. Its stdout is one JSON object: `{"key_file": "<path>", "entry": {id, sha256, actor, scopes, expires?}}`. There is no option that shows the secret.
- Agents must not print or read the key file into their context (for example with `cat`).
- Name key files `<id>.agent-key`. The generated `.gitignore` ignores `*.agent-key`.
- Revocation: remove the entry from `AGENT_CREDENTIALS`. Render restarts the service.
- Rotation: add a new entry, switch the client, then remove the old entry.

### Errors (`detail` strings, checked in this order)

| Status | `detail` | Cause |
|---|---|---|
| 503 | `auth_not_configured` | Google sign-in settings are missing. Applies to every request and every token. |
| 401 | `machine_auth_not_configured` | A Bearer header is sent and `AGENT_CREDENTIALS` is empty. |
| 503 | `agent_credentials_invalid` | `AGENT_CREDENTIALS` cannot be parsed or fails a rule above. Browser sign-in still works. |
| 401 | `invalid_agent_credential` | Not the `agk` format, unknown ID, or hash mismatch. Also every Google token and every Logto JWT. |
| 401 | `agent_credential_expired` | The entry's `expires` instant has been reached (now >= expires, UTC). |
| 403 | `missing_actor_sub_header` | No `X-<APP>-Actor-Sub` header. |
| 403 | `actor_mismatch` | The header differs from the key's bound actor. |
| 403 | `missing_scope` | A write method with a read-only key. |

The client copies each `detail` string into its `error_code`.

## Delivery

- The Google auth PR adds the server path and its HTTP tests.
- The CLI and MCP PR adds the client path, its tests against these error strings, and an end-to-end test of a generated Google app through the CLI and the MCP server.
- The CLI and MCP PR is stacked on the Google auth branch, so the end-to-end test runs with both changes and does not skip.
- The run record names both commit identities.
- Neither PR claims that the Google-default CLI and MCP path works until both PRs merge.
