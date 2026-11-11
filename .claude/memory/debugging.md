# Debugging Gotchas

## Logto JWT: ES384, Not RS256
Logto uses ES384 (EC P-384) for JWT signing, not RS256. JWKS validation code must accept EC key types and the ES384 algorithm. This is an immutable Logto constraint.
Source: a past review record

## Logto Usernames Reject Hyphens
Logto's Management API validates usernames against `^[A-Za-z_]\w*` — hyphens are rejected with HTTP 400 on POST /api/users. App slugs are hyphenated (`my-sample-notes-app`), so sanitize before creating the M2M/user: replace `-` with `_` (e.g. prefix + `slug.replace("-", "_")`).
Source: my-sample-notes-app live bootstrap (2026-07-15)

## PyJWKClient SSL on macOS
PyJWKClient uses urllib (not requests) internally. On macOS, this causes SSL certificate validation failures. Fix: `ssl.create_default_context(cafile=certifi.where())` passed to PyJWKClient constructor.
Source: a past review record

## requests.Session.timeout Is Non-Functional
Setting `session.timeout = 30` does NOT work — the `timeout` attribute on `requests.Session` is silently ignored. Must pass `timeout=30` on every individual `session.get()` / `session.post()` call.
Source: a past review record

## bash 3.2 on macOS
macOS ships with bash 3.2. No `declare -A` (associative arrays), no `readarray`, no `mapfile`. Use temp files or grep-based lookups for state management in shell scripts.
Source: a past review record

## Copier Venv Binary Path
`copier` installed in a venv must be invoked via full path (`.venv/bin/copier`) in subprocess calls. The system PATH does not include the venv by default.
Source: a past review record

## Env Var Naming Specificity
Use purpose-specific names like `LOGTO_M2M_APP_ID` instead of generic `M2M_CLIENT_ID`. When multiple apps share an environment, generic names cause silent collisions.
Source: a past review record
