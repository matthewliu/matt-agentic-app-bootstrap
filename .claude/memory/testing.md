# Testing Patterns

## Copier Template Testing
- Copier requires `--skip-tasks` and `--defaults` flags for non-interactive test runs
- Files with `{{ }}` Jinja syntax must use `.jinja` extension or Copier treats them as static
- Copier venv binary must use full path (`.venv/bin/copier`) in subprocess calls
Source: a past review record

## Template Rendering vs Execution Tests
Verifying template output strings (generated code correctness) is more durable than running the full generated app (requires all deps, services, env setup). 246 tests run in ~120s with this approach.
Source: a past review record

## Mocks Validate Your Assumed CLI Semantics, Not the Real Integration
The rendering-test lean above is correct for TEMPLATE CORRECTNESS but does NOT cover ORCHESTRATION against external CLIs/APIs (Railway, Logto, pnpm, Vite). Mock/string tests encode your *assumption* of how those tools behave, so a green suite means "the model is internally consistent," not "the integration works." The 2026-07-15 my-sample-notes-app run surfaced ~10 integration bugs (railway link/deploy semantics, pnpm 10 build scripts, VITE build-time vars, DATABASE_URL, Logto username regex) that ALL passed their mocks. Rule: any external-CLI integration step needs a live end-to-end smoke run before it's trusted. `AGENTIC_CORE_DEBUG=1` logs exact argv + full stdout/stderr — use it when a command "can't find" a resource that demonstrably exists.
Source: my-sample-notes-app live bootstrap (2026-07-15)

## Resume/State-Machine Behavioral Testing
Resume and retry paths need subprocess-level behavioral tests (actually running the script), not just string-presence checks that verify the code text exists. No test coverage of `--resume` path currently.
Source: a past review record

## Integration Tests Are Worth the Time
E2E test of `copier copy` + `pip install` + `pytest` catches real integration issues that unit-level arg parsing misses. 15s runtime is an acceptable tradeoff.
Source: a past review record

## FastAPI Dependency Override Cleanup
When tests apply `app.dependency_overrides`, must restore to original state or use fixtures with proper teardown to avoid global-state pollution across test cases.
Source: a past review record

