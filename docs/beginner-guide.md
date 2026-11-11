# Beginner guide: the tools and ideas behind each app

This guide is for people who are new to coding. It explains the tools that every app from `agentic-core` uses, and how to install them. Your coding agent can read this guide to explain a concept to you. For example, ask your agent: "Read `docs/beginner-guide.md` and explain what a virtual environment is."

You do not have to read the whole guide. Use the table of contents and read the part you need.

- [The parts of an app](#the-parts-of-an-app)
- [Install the tools](#install-the-tools)
- [Python and virtual environments](#python-and-virtual-environments)
- [Postgres on your computer](#postgres-on-your-computer)
- [Run an app on your computer](#run-an-app-on-your-computer)
- [FastAPI: how a request becomes a response](#fastapi-how-a-request-becomes-a-response)
- [Add a feature for people and agents](#add-a-feature-for-people-and-agents)
- [Settings and secrets](#settings-and-secrets)
- [Glossary](#glossary)

## The parts of an app

Each app has one API. People use the API through the web app. AI agents use the same API through the CLI or the MCP server. Only the API reads and writes the database.

![One API for people and agents: a person uses the web app, an AI agent uses the CLI or the MCP server, and all three call one FastAPI API that alone reads and writes Postgres.](diagrams/one-api-three-doors.svg)

| Part | What it is | Where it lives in an app |
| --- | --- | --- |
| API | A program that answers requests over HTTP. Each request goes to a path such as `/api/items`. | `app/` |
| FastAPI | The Python library that the API uses. It checks request data and makes the API reference page at `/docs`. | `app/main.py`, `app/routes/` |
| Database | Postgres. It keeps the data after the app stops. | Hosted by Render, or local in Docker |
| Web app | The pages that a person opens in a browser. It is built with React and Vite. | `frontend/` |
| CLI | A command-line tool, for example `my-app items list`. People and agents run it in a terminal. | `app/cli/` |
| MCP server | A program that gives an AI agent a list of tools. Each tool calls the API. | `app/mcp_server/` |
| Sign-in | Google sign-in for people. Agent keys for agents. | `app/auth/`, `app/routes/auth.py` |

## Install the tools

Install each tool once. The commands are for macOS with [Homebrew](https://brew.sh) and for Ubuntu Linux. On Windows, install [WSL](https://learn.microsoft.com/windows/wsl/install) first, open the Ubuntu terminal, and use the Ubuntu commands.

| Tool | What it does | macOS | Ubuntu | Check |
| --- | --- | --- | --- | --- |
| Python 3.12 or newer | Runs `agentic-core` and the app backend | `brew install python@3.12` | `sudo apt install python3 python3-venv` (Ubuntu 24.04 has 3.12) | `python3 --version` |
| Git | Keeps the history of your code | `brew install git` | `sudo apt install git` | `git --version` |
| Node.js 22 or newer | Builds and runs the web app | `brew install node@22` | See [nodejs.org](https://nodejs.org/en/download) | `node --version` |
| pnpm 10 | Installs the web app packages | `npm install -g pnpm@10` | `npm install -g pnpm@10` | `pnpm --version` |
| Docker | Runs Postgres on your computer | [Docker Desktop](https://www.docker.com/products/docker-desktop/) | [Docker Engine](https://docs.docker.com/engine/install/ubuntu/) | `docker compose version` |
| GitHub CLI | Creates the GitHub repo for a new app | `brew install gh`, then `gh auth login` | See [cli.github.com](https://cli.github.com) | `gh auth status` |
| Render CLI (optional) | Checks `render.yaml` before you deploy | See [Render CLI docs](https://render.com/docs/cli) | See [Render CLI docs](https://render.com/docs/cli) | `render whoami` |

If `python3 --version` shows a version older than 3.12, you can also use [uv](https://docs.astral.sh/uv/). It installs the correct Python for you: `uv venv --python 3.12 --seed .venv`.

## Python and virtual environments

A **virtual environment** is a folder that holds one copy of Python and the packages for one project. Each app has its own virtual environment in the `.venv` folder. Then two apps can use different package versions, and nothing changes the Python on your computer.

Make the virtual environment once, in the app folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

- `python3 -m venv .venv` makes the `.venv` folder.
- `pip` installs packages. `-e ".[dev]"` installs the app itself and its development tools, such as `pytest` and `ruff`.
- To run a tool from the virtual environment, put `.venv/bin/` before its name, for example `.venv/bin/pytest`.
- Or type `source .venv/bin/activate` once. Then `pytest` and `uvicorn` work without the prefix until you close the terminal.

Git ignores `.venv`. If the folder breaks, delete it and make it again.

## Postgres on your computer

Postgres is the database. Your deployed app uses a database that Render (or Railway) runs for you. On your computer, you need your own local Postgres to try features that save data.

The tests do not need Postgres. They use a temporary SQLite database in memory.

### Option A: Docker (recommended)

Each app with a database has a `compose.yaml` file. It starts Postgres 16, the same major version that Render uses.

```bash
docker compose up -d db      # start Postgres in the background
docker compose ps            # wait until STATUS shows "healthy"
```

- The `.env.example` file already holds the matching `DATABASE_URL`: `postgresql://postgres:postgres@localhost:5432/<app-name>`. These are fixed local values, not secrets.
- Postgres listens on `127.0.0.1` only, so other computers on your network cannot connect.
- `docker compose down` stops Postgres. Your data stays.
- `docker compose down -v` deletes all local data.
- If port 5432 is in use (for example, by another Postgres), change `"127.0.0.1:5432:5432"` to `"127.0.0.1:5433:5432"` in `compose.yaml` and change the port in `DATABASE_URL` to `5433`.

### Option B: Postgres from Homebrew (macOS)

```bash
brew install postgresql@16
brew services start postgresql@16
createdb my-app
```

Then set `DATABASE_URL=postgresql://localhost:5432/my-app` in `.env`. Homebrew Postgres uses your macOS user name and no password.

### Create the tables

A **migration** is a script that creates or changes database tables. The app uses Alembic for migrations. Run all migrations with one command:

```bash
.venv/bin/my-app-migrate
```

Replace `my-app` with the app name. The command reads `DATABASE_URL` from `.env`. Run it again after you pull new migrations. On Render, the same command runs before each deploy.

## Run an app on your computer

![Run an app on your computer: the browser opens the Vite dev server on port 5173, which forwards /api and /auth to FastAPI on port 8000, which uses Postgres in Docker on port 5432 and reads settings from the .env file.](diagrams/local-dev-setup.svg)

Use two terminals, both in the app folder.

Terminal 1, the backend:

```bash
cp .env.example .env              # once; then edit .env
docker compose up -d db           # start Postgres
.venv/bin/my-app-migrate          # create the tables
.venv/bin/uvicorn app.main:app --reload
```

Terminal 2, the web app:

```bash
pnpm install                      # once
pnpm -C frontend dev
```

Open http://localhost:5173. The app shows "Sign-in is not configured" until you set up Google sign-in. Follow the [Google sign-in walkthrough](google-oauth-walkthrough.md).

- `--reload` restarts the backend each time you save a Python file.
- The Vite dev server reloads the page each time you save a frontend file.
- http://localhost:8000/docs shows every API route.
- Press Ctrl+C in a terminal to stop its server.

## FastAPI: how a request becomes a response

A request goes through the same layers each time:

| Step | Layer | Folder | Job |
| --- | --- | --- | --- |
| 1 | Sign-in check | `app/auth/` | Finds who sent the request. Rejects it if nobody is signed in. |
| 2 | Route | `app/routes/` | Reads the path and the request data. Keep it short. |
| 3 | Request and response models | `app/models/` | Pydantic classes. They check the data shape and make the `/docs` page. |
| 4 | Service | `app/services/` | The business logic. |
| 5 | Database models | `app/db/models.py` | SQLAlchemy classes. Each class is one table. |

The API takes the user ID from the sign-in check, never from the request body. Each query filters by that user ID, so each person sees only their own data.

![Starting data model: user_profiles has one row per person, keyed by user_id such as google:1234567890; items has many rows per person, linked by user_id.](diagrams/data-model.svg)

The app starts with two tables. `user_profiles` has one row for each person. `items` is a sample table. Replace it or add tables for your own app. After you change `app/db/models.py`, make a migration:

```bash
.venv/bin/alembic -c app/db/migrations/alembic.ini revision --autogenerate -m "add notes table"
.venv/bin/my-app-migrate
```

Read the new file in `app/db/migrations/versions/` before you run it. Autogenerate can miss some changes, such as a renamed column.

## Add a feature for people and agents

Each feature must work for people and for agents. When you add a route, also add it to the CLI and, if agents need it, to the MCP server:

1. Add the route in `app/routes/` with an `operation_id`, for example `notes_list`.
2. Add a matching `Operation` in `app/cli/operations.py`.
3. Add a CLI command in `app/cli/commands/`.
4. Add an MCP tool in `app/mcp_server/server.py` only if agents need it. A tool that writes stays off until `<APP>_MCP_ALLOW_WRITES=1`. Never add a delete tool.
5. Add tests for each part.

The CLI and the MCP server call the HTTP API. They never open the database. So the API's sign-in check and per-user filter protect every caller the same way. See [architecture.md](architecture.md) for the full picture.

## Settings and secrets

An **environment variable** is a named setting that a program reads when it starts, for example `DATABASE_URL`. On your computer, the app reads them from the `.env` file. On Render, you set them in the service's **Environment** page.

- `.env.example` lists every setting with safe example values. Copy it to `.env`.
- Git ignores `.env`. Never commit it. Never paste a secret into a chat with an agent.
- A **secret** is a value that gives access, for example `GOOGLE_CLIENT_SECRET`, `SESSION_SECRET`, or an agent key. Type each secret yourself into `.env` or into the Render dashboard.
- Use different Google clients and secrets on your computer and on Render.
- An agent key lives in its own file, for example `cli.agent-key`. Git ignores `*.agent-key`. Never `cat` the file or paste it into a chat.

## Glossary

| Term | Meaning |
| --- | --- |
| Agent key | A secret for one agent. It acts for one person and reads only, unless the operator allows writes. Format: `agk.<id>.<secret>`. |
| Alembic | The migration tool for the database. |
| Blueprint | Render's name for `render.yaml`. It lists the web service and the database to create. |
| Client ID and client secret | The name and password of your app at Google. The ID is public. The secret is not. |
| CSRF token | A random value that the web app sends with each change request. It stops other websites from making changes as you. |
| Docker | A tool that runs programs, such as Postgres, in isolated containers. |
| Environment variable | A named setting that a program reads at start-up. |
| MCP | Model Context Protocol. A standard way for an AI agent to find and call tools. |
| Migration | A script that creates or changes database tables. |
| OAuth and OpenID Connect | The standards behind "Sign in with Google". |
| OpenAPI | A machine-readable description of the API. FastAPI makes it, and `/docs` shows it. |
| ORM | Object-relational mapper. SQLAlchemy turns Python classes into tables and rows. |
| Redirect URI | The address that Google sends the browser back to after sign-in, for example `http://localhost:8000/auth/callback`. |
| Session cookie | A small signed value that the browser keeps after sign-in. The browser sends it with each request. |
| Virtual environment | A project folder (`.venv`) that holds Python and the project's packages. |
