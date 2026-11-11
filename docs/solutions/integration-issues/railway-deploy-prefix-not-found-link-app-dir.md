---
title: "create-app appeared hung and silently accepted failed external provisioning"
module: bootstrap
date: 2026-07-15
last_updated: 2026-08-07
problem_type: integration_issue
component: tooling
severity: high
symptoms:
  - "After the admin keystore passphrase, create-app printed no progress and appeared to hang"
  - "A stale Railway link in agentic-core caused RAILWAY_PROJECT_MISMATCH on the next bootstrap"
  - "Railway required an explicit workspace during non-interactive project creation"
  - "A detached upload was treated as success even when the Railway build failed"
  - "Multiline descriptions, ignored --no-mobile, and a swallowed SSH push failure produced a broken app"
root_cause: missing_workflow_step
resolution_type: code_fix
related_components:
  - railway
  - orchestrator
  - github
  - cli
  - template
tags:
  - create-app
  - bootstrap
  - progress-output
  - railway-cli
  - cwd-link
  - deployment-status
  - github-push
  - toml
---

# create-app appeared hung and silently accepted failed external provisioning

> **Legacy.** This fix applies to the Railway target (`--deploy-target railway`). New apps deploy on Render.

## Problem

`agentic-core create-app` could appear frozen immediately after the admin
keystore prompt while it made slow external calls. The missing feedback hid a
set of integration failures: Railway commands inherited the `agentic-core`
working directory, workspace selection was unreliable in non-interactive runs,
upload acceptance was mistaken for deployment success, multiline descriptions
could break TOML, `--no-mobile` was ignored, and Git push failures were swallowed.

## Symptoms

- A human-mode command printed nothing for long stretches after the keystore
  passphrase. Human mode now supplies the console printer as the progress
  callback, while JSON mode supplies `None` to keep stdout machine-readable
  (`agentic_core/cli.py:510-525`).
- A later scaffold failed with `RAILWAY_PROJECT_MISMATCH` because the previous
  project's directory-scoped Railway link had contaminated `agentic-core`. The
  target app directory is now created before provisioning and supplied as the
  Railway `cwd` (`agentic_core/bootstrap/orchestrator.py:528-540`).
- Accounts with multiple Railway workspaces failed with
  `--workspace required in non-interactive mode`. Workspace resolution now
  happens after `.env` loading (`agentic_core/cli.py:453-468`). A later
  change removed the built-in workspace name: an account with several
  workspaces must pass `--railway-workspace` or set `RAILWAY_WORKSPACE`.
- `railway up -d` returned after upload, a remote build failed, and the pipeline
  spent minutes health-polling an app that could not start. Deployment terminal
  state is now polled separately (`agentic_core/bootstrap/railway.py:875-960`).
- A multiline description generated invalid `pyproject.toml`; whitespace is
  now normalized at the orchestration boundary
  (`agentic_core/bootstrap/orchestrator.py:470-477`).
- `--no-mobile` was accepted but an Expo app was still rendered. The CLI now
  passes `with_mobile=False` through to Copier
  (`agentic_core/cli.py:446-451`,
  `agentic_core/bootstrap/orchestrator.py:1402-1413`).
- The initial push used an SSH remote, could wait on SSH authentication, and
  was checkpointed even after failure. Git now uses checked HTTPS operations
  with interactive prompts disabled (`agentic_core/bootstrap/github.py:160-272`).

## What Didn't Work

Running `railway unlink` in `agentic-core` repaired one local link but did not
repair the bootstrap. The next `railway init` from the same wrong directory
would recreate the contamination. An end-of-run unlink has the same crash
window and treats the symptom rather than the directory ownership error.

Passing more target flags only at deploy time was also insufficient. Railway
commands derive important context from their working directory, so the
directory invariant must start with project lookup/creation and continue
through variables, database, domain, link, upload, and deployment polling.

Detached upload was not a valid success gate. `railway up -d` confirms that the
upload was accepted; it does not prove that the remote image built or deployed.
A later health timeout hid the actual build failure.

The old best-effort Git loop was actively misleading. It used
`git@github.com:...` in a non-interactive pipeline and ignored return codes, so
`git_commit` could be marked complete even when GitHub had no code.

Finally, green mocks were not enough. External-CLI mocks prove that the wrapper
matches the assumed command contract; only a live smoke run proves that the
installed Railway and GitHub CLIs still interpret it that way.

## Solution

### Restore an operator-visible event stream

Every completed checkpoint emits through the same `_mark` path that persists
resume state (`agentic_core/bootstrap/orchestrator.py:608-627`). Slow Git and
Railway operations announce themselves before waiting, and Railway emits a line
when its status changes. Human output now resembles:

```text
✓ Admin keystore unlocked
✓ Logto resources provisioned
✓ GitHub repository ready
✓ Railway project and service ready
✓ Application template rendered
→ Creating and pushing the initial Git commit...
✓ Initial Git commit prepared
→ Uploading the application to Railway...
→ Waiting for Railway to build and deploy...
    Railway deployment status: BUILDING
    Railway deployment status: DEPLOYING
    Railway deployment status: SUCCESS
✓ Railway deployment succeeded
→ Verifying the production deployment...
✓ Deployment verification passed
Scaffolded <app-slug> (...)
```

The full output and operator workflow are documented in
[Bootstrap Process Flow](../../bootstrap-process-flow.md).

### Scope every Railway command to the app

The orchestrator creates `<output_dir>/<slug>` before Railway provisioning and
passes it as `cwd` to every directory-sensitive wrapper. Project adoption also
requires the linked project name to match the requested slug. This prevents the
non-deployable core checkout from acquiring an app link and prevents a later
run from adopting another app's project.

Workspace selection is explicit: CLI flag, then `RAILWAY_WORKSPACE`. With
either one set, the project is created without an interactive workspace
prompt.

### Separate upload, deploy, and application health

After `railway up -d`, `wait_for_deployment` queries:

```text
railway deployment list --service <slug> --json
```

It returns success only for `SUCCESS`. Terminal failures return
`RAILWAY_DEPLOY_FAILED` with the deployment ID and build-log command. Only a
successful Railway deployment advances to health/auth verification
(`agentic_core/bootstrap/orchestrator.py:1529-1559`).

### Make Git non-interactive and checked

`initialize_and_push_repo` uses the HTTPS URL returned by GitHub, sets
`GIT_TERMINAL_PROMPT=0`, updates an existing remote on resume, and checks every
meaningful command. A timeout or non-zero push preserves retryable state instead
of recording a false checkpoint (`agentic_core/bootstrap/github.py:160-272`).

### Normalize generation inputs

Descriptions are collapsed to a single whitespace-normalized line before they
reach GitHub or Copier. Frontend/mobile switches are threaded as explicit
orchestrator booleans into Copier's `include_frontend` and `include_mobile`
values.

## Why This Works

The apparent hang and the provisioning bugs shared a pattern: orchestration
state was inferred from ambient process context or a local subprocess return
instead of being modeled explicitly.

- Visible checkpoint completion and persisted checkpoint completion now share
  one path, so the UI cannot silently drift from resume state.
- Railway identity comes from the target app directory, not the shell that
  happened to invoke the CLI.
- Workspace selection happens after repository environment files are loaded.
- Upload acceptance, remote deployment success, and live application health
  are three separate gates.
- Git cannot open a hidden prompt and cannot advance after a failed push.
- Free-form text and feature flags are normalized once at the template boundary.

The repair was verified against the live `my-sample-app` resources:
Railway visibly moved through `BUILDING → DEPLOYING → SUCCESS`, production
`/health` returned the expected app/version payload, and the generated GitHub
repository received its initial commit over HTTPS.

## Prevention

- Treat progress output as an operator-facing API. The CLI test invokes the
  Typer command and asserts representative progress appears before the final
  summary; a second test proves JSON mode remains exactly one JSON document
  (`tests/unit/test_agentic_core_cli.py`).
- Require `<output_dir>/<slug>` as `cwd` on every new Railway wrapper call.
  Tests enumerate the directory-scoped calls
  (`tests/unit/test_bootstrap_orchestrator.py`).
- Test both halves of detached deployment: terminal-status behavior in the
  Railway wrapper and the orchestrator rule that a failed build never reaches
  health verification (`tests/unit/test_bootstrap_railway.py`,
  `tests/unit/test_bootstrap_orchestrator.py`).
- Parse a rendered `pyproject.toml` during smoke validation and verify excluded
  feature directories are absent.
- Keep Git HTTPS-only, non-interactive, and fail-loud in unattended workflows
  (`tests/unit/test_bootstrap_github.py`).
- Run a live external-CLI smoke test after changing Railway or GitHub
  orchestration. Use `AGENTIC_CORE_DEBUG=1` to capture exact Railway argv, cwd,
  exit code, stdout, and stderr (`agentic_core/bootstrap/railway.py:30-59`).

Focused regression coverage:

```bash
.venv/bin/python -m pytest \
  tests/unit/test_agentic_core_cli.py \
  tests/unit/test_bootstrap_orchestrator.py \
  tests/unit/test_bootstrap_railway.py \
  tests/unit/test_bootstrap_github.py \
  tests/integration/test_bootstrap_end_to_end.py \
  tests/integration/test_bootstrap_resume.py -q
```

## Related Issues

- [Bootstrap Process Flow](../../bootstrap-process-flow.md) — current operator
  reference, expected output, recovery, and safety invariants.
