# Security Patterns

## Shell Injection via eval()
Never use `eval "$output"` on external API output. Even "trusted" Python scripts ultimately carry data from external APIs (Logto, Railway). Parse with `read`, `cut`, or `grep` instead.
Source: a past review record

## Secrets in CLI Arguments
`RAILWAY_API_TOKEN` passed as curl `-H` flag is visible to `ps aux` and `/proc` on shared/CI machines. Pass secrets via environment variables to inline Python, never as command-line arguments.
Source: a past review record (P0, 3 reviewers)

## Path Traversal Protection
`Path() / user_path` does NOT neutralize `..` segments. Must use `resolve()` then `is_relative_to()` then `is_file()` — all three steps required. Three reviewers flagged as P0.
Source: a past review record

## Production URLs as Defaults
Never hardcode live production URLs (e.g., Logto endpoint) as template defaults or fallbacks. Every generated app would silently validate against production on missing env vars. Use empty string + fail-fast.
Source: a past review record

## CORS Environment Separation
`localhost` CORS origins belong only in local `.env`, never in production Railway env vars. Bootstrap script correctly sets production CORS to domain-only. Multiple security reviewers flagged as defense-in-depth.
Source: a past review record

## RAILWAY_REGION Validation
Environment variables interpolated into JSON GraphQL strings must be validated (e.g., `^[a-z0-9-]+$`) before use. Without validation, a malformed value corrupts the mutation payload.
Source: a past review record
