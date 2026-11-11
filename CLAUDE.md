# Agentic Bootstrap (`agentic-core`)

`agentic-core` is a Python CLI and a Copier template. One command creates a new FastAPI app with Google sign-in, a React frontend, a CLI, an MCP server, tests, CI, and a Render Blueprint. This repo is not a deployable app. Read `README.md` for the user view and `PRODUCT.md` for scope.

**Core principle:** every generated app serves people and agents with one set of rules. The web app, the CLI, and the MCP server all call the same typed API, and only the API opens the database. Keep this true in every template change. `docs/architecture.md` explains it.

## Communication and documentation

- Use plain English in replies. Lead with the answer. Keep replies short.
- Write technical docs in ASD-STE100-style Simplified Technical English: one idea per sentence, one action per instruction, one meaning per term.
- Keep exact code, commands, API names, and quoted text.
- Many readers are new to coding. Explain a technical term the first time you use it.

## Commands (verified)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"     # needs 3.12+; else python3.12 or `uv venv --python 3.12 --seed .venv`
.venv/bin/python -m pytest tests/ -x -q                          # full suite (integration tests need network for pip)
.venv/bin/python -m pytest tests/unit -q                         # faster, no app installs
.venv/bin/ruff check .                                           # lint

.venv/bin/agentic-core create-app <name> --no-github --no-deploy # local app only
.venv/bin/agentic-core create-app <name> --description "..."     # GitHub repo + validated render.yaml
.venv/bin/agentic-core create-app <name> --dry-run               # plan only, no changes
.venv/bin/agentic-core create-app <name> --preflight             # check logins and tools only
.venv/bin/agentic-core --json create-app <name> --scaffolded-by agent
```

## Defaults

| Choice | Default | Other option |
| --- | --- | --- |
| Hosting | Render: one `render.yaml` Blueprint per app. The person applies it; the CLI creates no paid resource. | `--deploy-target railway` (risky; creates paid resources itself) |
| Sign-in | Google OpenID Connect, server-side, signed HttpOnly session cookie, `AUTH_ALLOWED_EMAILS` allowlist | `--auth-provider logto` (supported; self-hosted Logto; needed for the mobile app) |
| Agent auth | Per-app agent keys (`agk.<id>.<secret>`, hashes in `AGENT_CREDENTIALS`) | Logto M2M client credentials |
| Data | One Postgres per app | — |
| Design | One token file per app: `frontend/src/design/tokens.ts`, explained in the app's `DESIGN.md` | — |

Render became the default on 2026-11-05 after Railway's October 2026 environment-variable incident. Google sign-in became the default on 2026-11-10 (PR #10). Sign-in and hosting are independent choices since PR #12.

## Security boundaries (call out any change to these)

- A Google app shares nothing with other apps: own OAuth client, session secret, allowlist, and agent keys.
- Never accept a Google token as a machine credential. Agents use agent keys only.
- Protected routes return 503 `auth_not_configured` until the operator sets the Google settings. Do not weaken this.
- Never put a literal secret `value` in `render.yaml`. Keep `ipAllowList: []` on databases. The generated `tests/unit/test_render_blueprint.py` checks both.
- Logto apps share one Logto tenant for people; tokens, clients, agent credentials, and allowlists stay per-app. See `docs/SECURITY.md`.

## Rules a new agent would not guess

- **Keep Logto output byte-identical when you change template auth.** Gate Google-only text with inline Jinja (`{% if auth_provider == 'google' %}line\n{% endif %}`). Copier does not trim block tags, so a tag on its own line adds a blank line to Logto apps.
- **The deploy target comes from command-line flags only.** A `RAILWAY_WORKSPACE` value in `.env` never selects Railway. `--no-railway` or `--railway-workspace` on the command line still select Railway, so existing agent scripts keep working.
- **The Copier default for `auth_provider` stays `logto`.** The CLI passes `google`. This stops `copier update` from changing old apps.
- **Generated `CLAUDE.md` must stay under 100 lines.** `tests/unit/test_claude_scaffolding.py` checks it.
- **Design values live only in the app's `frontend/src/design/tokens.ts`.** `tailwind.config.ts` reads it; tests reject hex colors in `index.css`.
- **Diagrams use the `diagram-design` skill, never Mermaid.** Each diagram is a `.html` source plus an exported `.svg` in `docs/diagrams/`. The style is `docs/diagrams/diagram-style.md` (marker: `.diagram-design`). Some diagrams also ship in the template; `tests/unit/test_template_diagrams.py` fails when the copies differ.
- **Mocks do not prove a CLI integration.** Run one live smoke test for a change to `gh`, `render`, or `railway` calls. See `.claude/memory/testing.md`.
- **No owner-specific defaults.** Other people run this CLI. The Logto domain and endpoint, the Railway workspace, the keystore path, and the GitHub owner come from flags, `.env`, or the logged-in CLI, never from a built-in value.
- **Do not delete the `agentic-core-admin` app in Logto.** Automated Logto setup depends on it. See `docs/logto-setup-guide.md`.

## Key directories

| Path | Contents |
| --- | --- |
| `agentic_core/cli.py` | Typer entry point: `create-app`, `list-apps`, `agent-context`, `teardown`, `admin` |
| `agentic_core/bootstrap/` | Orchestrator, preflight, GitHub, Render, Railway, Logto, keystore, teardown |
| `templates/mini-app/` | Copier template (`copier.yml`) for every new app |
| `tests/unit/`, `tests/integration/`, `tests/contract/` | Template, CLI, and generated-app tests |
| `docs/` | Guides. `docs/README.md` sorts them into default, other options, and history. `docs/beginner-guide.md` explains concepts for new coders; use it when you explain something to a person. |
| `docs/diagrams/` | Diagram sources (`.html`) and images (`.svg`) |

## Knowledge stores

- `.claude/memory/MEMORY.md`: topic index of cross-cutting learnings. Read it before you work in a listed area.
- `docs/solutions/`: solved-bug writeups with exact error strings.
- Do not write session diaries or shipped-PR summaries in either store. Git history and PR descriptions hold those.

## Sibling apps

Apps are created as sibling folders of this repo, each with its own GitHub repo. To check whether a template bug is systemic, look in every sibling app folder (`ls ..`).
