# Bootstrap Process Flow

This is the operator reference for the Python `agentic-core create-app` pipeline.

## Render target (default since 2026-11-05)

With no deploy flags, the run uses the Render target. It has five steps:

1. Preflight: the `render` CLI is on `PATH`, `render whoami` succeeds, and `render workspace current` returns the active workspace.
2. Template: Copier renders the app with `render.yaml`, no `railway.json`, and Google sign-in (`auth_provider=google`). Logto is not provisioned and the keystore is not opened.
3. Validate: `render blueprints validate render.yaml -w <workspace-id> -o json` runs live. An invalid Blueprint stops the run before any GitHub change.
4. GitHub (optional): the repo is created and `main` is pushed.
5. Result: `render.status` is `awaiting_blueprint_apply`, `auth.status` is `not_configured`, and `render.blueprint_url` is the `https://render.com/deploy?repo=...` link when the run created the repo.

The operator applies the Blueprint from that link or from New > Blueprint in the Render dashboard. That click creates the billable resources. If no deploy starts, start one manual deploy. See the [README](../README.md#create-an-app-with-github-and-render) for cost. To turn on sign-in, follow the generated app's README, section "Google sign-in", and [google-auth-setup-guide.md](google-auth-setup-guide.md).

## Railway target (opt-in)

**Other option, with risk.** The rest of this document describes the Railway target (`--deploy-target railway`, or `--no-railway` / `--railway-workspace` on the command line). The default Render target does not use it. Read [choosing-options.md](choosing-options.md) for the risks first. That pipeline provisions one sibling application end to end: Logto, GitHub, Railway, Postgres, template rendering, the initial push, deployment, and production verification.

Sign-in and hosting are independent choices. With `--deploy-target railway` alone, the app uses Google sign-in and the pipeline skips step 3. Add `--auth-provider logto` for the automated Logto setup.

For command examples, see the [README](../README.md#useful-options). Run `agentic-core create-app --help` for every flag.

## Running a Bootstrap

Run the CLI from `agentic-core`; all directory-scoped provider operations are
executed against the new app directory internally.

```bash
.venv/bin/agentic-core create-app my-sample-app \
  --description "Sample app" \
  --no-mobile
```

The target is a sibling directory:

```text
<folder that holds agentic-core>/my-sample-app/
```

`--no-frontend` and `--no-mobile` independently control the optional web and
Expo projects for the `full` variant. The description is normalized to one
line before it reaches GitHub metadata or generated TOML.

## Progress Output

Human-mode commands print a completion line after every checkpoint and print
additional status before operations that may take noticeable time. A normal
run resembles:

```text
agentic-core admin keystore passphrase:
✓ Admin keystore unlocked
✓ Logto resources provisioned
✓ GitHub repository ready
✓ GitHub Actions secrets configured
✓ Railway project and service ready
✓ Railway environment configured
✓ Railway Postgres provisioned
✓ Production domain, CORS, and redirect URIs configured
✓ Application template rendered
→ Creating and pushing the initial Git commit...
✓ Initial Git commit prepared
→ Linking the app directory to Railway...
→ Uploading the application to Railway...
→ Waiting for Railway to build and deploy...
    Railway deployment status: BUILDING
    Railway deployment status: DEPLOYING
    Railway deployment status: SUCCESS
✓ Railway deployment succeeded
→ Verifying the production deployment...
✓ Deployment verification passed
Scaffolded my-sample-app (variant=full, dry_run=False)
```

Flags and resume checkpoints can omit lines for work that is intentionally
skipped. Silence after the passphrase is not expected: unlocking the keystore
produces the first checkmark, and later long-running Git/Railway operations
announce themselves before waiting.

`agentic-core --json create-app ...` deliberately suppresses these human lines
and writes exactly one machine-readable JSON envelope to stdout.

### Proving the output contract

The CLI-level regression test invokes the Typer command, makes the orchestrator
emit representative progress events, and asserts that they appear before the
final `Scaffolded ...` summary:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_agentic_core_cli.py::TestCreateAppFlags::test_human_mode_prints_orchestrator_progress \
  tests/unit/test_agentic_core_cli.py::TestCreateAppFlags::test_json_mode_suppresses_human_progress_callback \
  -q
```

The orchestrator suite separately verifies checkpoint messages, Railway status
updates, and the rule that a failed Railway build never falls through to health
polling.

## Pipeline Sequence

### 1. Validate inputs and preflight

- Validate the lowercase, hyphenated app slug and variant/flag combination.
- Load shared credentials and repository-local `.env` values without
  overriding variables already exported by the shell.
- Validate required CLIs, tokens, GitHub login, Railway login, and the admin
  keystore before provisioning.
- Resolve Railway workspace in this order: `--railway-workspace`, then
  `RAILWAY_WORKSPACE`. There is no built-in workspace. If your Railway account
  has more than one workspace, name one, or `railway init` fails.
- For Logto, resolve the platform domain in this order: `--platform-domain`,
  then `PLATFORM_DOMAIN`. There is no built-in domain. A Logto run without
  one fails preflight.

Use `--preflight` to run only these checks, or `--dry-run` to preview the
resolved configuration without external calls.

### 2. Load resumable state

The target app owns `.bootstrap-state.json`. It contains checkpoint booleans
and non-secret resource identifiers. It never stores provider secrets.

- A fresh run creates new state.
- `--resume` reads the state and skips completed checkpoints.
- Retryable failures preserve state for a later resume.
- Structural failures are marked non-retryable so resume cannot loop over a
  configuration that needs a code or operator fix.
- Full success deletes the state file.

### 3. Provision Logto

The CLI decrypts the `agentic-core-admin` keystore once and uses that credential
to provision the variant's SPA/M2M clients, API resource, scopes, and user.
The admin credential is zeroed after this phase. Per-app M2M secrets are pushed
to their destinations but never written to bootstrap state.

### 4. Create GitHub resources

The pipeline creates or adopts the private repository, then configures the
per-app Actions secrets. Adoption is name-specific and safe for resume.

### 5. Provision Railway from the app directory

Railway links are directory-scoped. Every Railway command receives the target
app directory as its working directory, including project creation. Therefore:

- `agentic-core` itself is never linked to an app project.
- `railway init` links the new sibling app directory.
- A later app cannot inherit the previous app's Railway project.
- An existing link is adopted only when its project name matches the app slug;
  a mismatch fails with `RAILWAY_PROJECT_MISMATCH` before variables, Postgres,
  or code can leak into the wrong project.

The pipeline creates/adopts the application service, pushes environment
variables, creates/adopts Postgres, captures the production domain, sets CORS,
and updates Logto redirect URIs.

### 6. Render the application

Copier renders the selected variant into the already-created app directory.
Feature flags are passed explicitly, so `--no-mobile` and `--no-frontend` affect
the generated tree instead of being accepted and ignored.

### 7. Commit and push over HTTPS

The generated app is initialized as a Git repository, committed, and pushed to
the HTTPS URL returned by GitHub. The Git wrapper:

- disables interactive credential prompts;
- updates an existing `origin` on resume;
- checks every Git command's exit status;
- preserves a retryable state checkpoint when commit or push fails.

A failed or timed-out push is never recorded as a successful checkpoint.

### 8. Deploy and watch Railway

The pipeline refreshes the app directory's explicit project/service/environment
link, uploads with `railway up -d`, and then polls the latest deployment until
it reaches a terminal state.

- `SUCCESS` advances to production verification.
- `FAILED`, `CRASHED`, `CANCELED`, and other failure states stop immediately
  with `RAILWAY_DEPLOY_FAILED` and a build-log command.
- A bounded deployment timeout returns `RAILWAY_DEPLOY_TIMEOUT`.

Upload acceptance is not treated as deployment success.

### 9. Verify production

After Railway reports `SUCCESS`, the verifier polls `/health`. On a fresh run,
while the per-app M2M secret is still available in memory, it also mints a token
and probes an authenticated route. A resumed run may perform health-only
verification because secrets are intentionally absent from the state file; the
result envelope reports whether authentication was checked.

Only after verification passes does the CLI delete bootstrap state and print
the final `Scaffolded ...` summary.

## Failure and Recovery

The failure message identifies the checkpoint, a stable error code, whether the
failure is retryable, and the state path. For a retryable failure:

```bash
.venv/bin/agentic-core create-app my-sample-app \
  --resume \
  --no-mobile
```

Use the same variant and feature flags as the original command. Completed
provider steps are skipped; only unfinished work is retried.

For exact Railway argv, cwd, exit codes, stdout, and stderr:

```bash
AGENTIC_CORE_DEBUG=1 .venv/bin/agentic-core create-app <app-name> ...
```

This is the first diagnostic to use for Railway CLI integration failures.
Mocks prove the wrapper follows the expected contract; a live smoke run proves
that the current external CLI still honors it.

## Safety Invariants

- The coordination repo is not a deployable service and must not retain a
  Railway project link.
- Provider adoption must match the requested app identity.
- External CLI success requires a checked zero exit code and, where available,
  parseable structured output.
- GitHub uses HTTPS and cannot prompt interactively during bootstrap.
- Railway build completion is observed before health verification begins.
- Human mode remains visibly active; JSON mode remains machine-parseable.
- Secrets never enter `.bootstrap-state.json`.
