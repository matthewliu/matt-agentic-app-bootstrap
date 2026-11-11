# Deployment Patterns

## Multi-Stage Dockerfile (Python + Node)
```
FROM node:22-slim AS frontend-build   # Build stage: npm ci + vite build
FROM python:3.12-slim                  # Runtime: pip install + COPY --from=frontend-build
```
Keeps Node (~200MB) out of production image. VITE_* env vars must be passed as Docker build ARGs since they're baked in at build time, not runtime.
Source: a past review record, feedback_fullstack_deploy

## Railway Region Configuration
Railway defaults to `asia-southeast1` if not explicitly set. Bootstrap script sets region via `serviceInstanceUpdate` GraphQL mutation after service creation. Default: `us-west1`. Override via `RAILWAY_REGION` env var.
Source: bootstrap-fixes session (2026-03-17)

## Railway Domain Parsing
`railway domain` CLI output can include decorative text (e.g., `ServiceDomaincreated:🚀https://...`). Extract hostname with `grep -oE '[a-z0-9-]+\.up\.railway\.app'` — never use naive prefix stripping.
Source: bootstrap-fixes session (2026-03-17)

## Railway Deploy Trigger
Every `railway variable set` call triggers a new deployment. The bootstrap intentionally triggers two deploys: first with env vars, second with CORS_ORIGINS (needs the domain from the first deploy). Docker image is cached so the second deploy is fast (~60s).
Source: a past review record

## Railway Deploy Is Directory-Scoped (link before `up`)
`railway up` resolves the target project/environment from the CURRENT DIRECTORY's link, not from flags. `railway init` links the dir it ran in (the orchestrator cwd), NOT the later-rendered app dir — so `railway up` from the app dir fails `prefix not found` / `No linked project found`, and `-p`/`-e` flags do NOT fix it (`--project` even requires `--environment` and still fails). Fix: `railway link -p <id> -s <service> -e production` IN the app dir, then bare `railway up -d`. Pass all three link flags so it stays non-interactive.
Source: my-sample-notes-app live bootstrap (2026-07-15); full writeup in docs/solutions/integration-issues/railway-deploy-prefix-not-found-link-app-dir.md

## Railway Does Not Auto-Inject DATABASE_URL
Adding a Postgres service does NOT expose `DATABASE_URL` to the app service. Set it explicitly as a reference var on the app service: `DATABASE_URL=${{Postgres.DATABASE_URL}}`. Without it the app crash-loops on boot (Starting/Stopping).
Source: my-sample-notes-app live bootstrap (2026-07-15)

## pnpm 10 Blocks Dependency Build Scripts
pnpm 10 refuses to run dependency build scripts (e.g. esbuild) by default → frontend build fails with `ERR_PNPM_IGNORED_BUILDS`. Do NOT downgrade pnpm. Approve the specific trusted scripts via `"pnpm": { "onlyBuiltDependencies": ["esbuild"] }` in the workspace package.json, and keep `npm i -g pnpm@10`.
Source: my-sample-notes-app live bootstrap (2026-07-15)

## VITE_* Must Be Pushed by the Pipeline, Not Just Declared
Declaring `ARG VITE_*` in the Dockerfile is necessary but not sufficient — Vite bakes `import.meta.env.VITE_*` at BUILD time from Railway service vars passed as build args. The bootstrap must actually SET `VITE_LOGTO_ENDPOINT`, `VITE_LOGTO_APP_ID` (the SPA client id), and `VITE_LOGTO_API_RESOURCE` on the service, or the frontend ships with empty auth config and renders blank.
Source: my-sample-notes-app live bootstrap (2026-07-15)

## tsc Blocks Docker Build
`npm run build` runs `tsc && vite build`. TypeScript errors in ANY file (including tests) block the entire Docker build. Template excludes test files from tsconfig.json via `exclude: ["src/**/__tests__/**"]`.

**Recurring patterns that cause deploy failures (hit in three early apps):**
- Unused imports: importing a hook/type and not using it (e.g., `import { useApi } from "./useApi"` when only `fetch` is used)
- Unused destructured params: `{ type, value, label, preview }` where `preview` isn't referenced in function body
- shadcn component prop mismatches: e.g., `asChild` not existing on `DialogTrigger` due to version differences
- These errors are **invisible during `npm run dev`** — Vite HMR doesn't run tsc. They only surface in the Docker build.

**Prevention:** Always run `cd frontend && npx tsc --noEmit` before deploying. This is now documented in the CLAUDE.md template (Deployment section) so future projects have the warning from day one.

Source: bootstrap-fixes session (2026-03-17), feedback_fullstack_deploy, an early deploy failure (2026-03-18)
