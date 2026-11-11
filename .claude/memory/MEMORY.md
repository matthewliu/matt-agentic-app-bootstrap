# Agentic Core - Project Learnings

Shared cross-cutting knowledge from building the Copier template, bootstrap script, and deployed apps.
This is a pointer index — do not inline topic content here, and do not save session diaries or shipped-PR snapshots.
Concrete solved-bug writeups with searchable error strings live in `docs/solutions/` (separate store).

## Topic Index
| File | Contents |
|------|----------|
| [security.md](security.md) | eval() injection, secrets in CLI args, path traversal, production URL defaults, CORS env separation |
| [patterns.md](patterns.md) | Idempotent 409 handling, shell+Python orchestration contracts, resume state validation, module-level side effects |
| [testing.md](testing.md) | Copier test flags, rendering vs execution tests, resume behavioral tests, mocks-vs-live-integration boundary, dependency override cleanup |
| [debugging.md](debugging.md) | Logto ES384 JWT, Logto username regex (no hyphens), PyJWKClient SSL on macOS, requests.Session.timeout, bash 3.2 |
| [deployment.md](deployment.md) | Multi-stage Dockerfile, Railway region/domain, directory-scoped deploy (link before up), DATABASE_URL reference var, pnpm 10 build scripts, VITE_* build args |
| [../../docs/lessons-mobile-deployment.md](../../docs/lessons-mobile-deployment.md) | EAS build profiles, New Arch disabled, native module compat, splash screen, KeyboardAvoidingView, EXPO_PUBLIC env vars, shared type sync, eager loading |
