# Product: Agentic Bootstrap

## Purpose

Agentic Bootstrap (`agentic-core`) turns one command into a working, tested web app. A person uses it in a browser. AI agents use the same app through its CLI and MCP server.

This is the main reason the project exists. Each app has one typed FastAPI API. The web app, the CLI, and the MCP server all call it, and none of them opens the database. So people and agents get the same sign-in, data, and input rules from the first commit.

## Users

- The maintainer, who starts new apps often.
- Friends who are new to coding. They build with coding agents and follow a setup runbook.
- Coding agents that run the command for a person and read its JSON result.

## What a new app must have

- A typed FastAPI API that a person, a CLI, and an MCP server all use with the same rules.
- Google sign-in for people. Agent keys for agents. No data before sign-in is set up.
- One Postgres database per app.
- One design-token source with light mode, dark mode, and phone, tablet, and desktop breakpoints.
- Tests and CI that pass on the first run.
- A Render Blueprint (`render.yaml`) that the person applies. The command never creates paid resources.

## Defaults

| Choice | Default | Other option |
| --- | --- | --- |
| Hosting | Render | Railway (`--deploy-target railway`). Risky: see `docs/choosing-options.md`. |
| Sign-in | Google | Logto (`--auth-provider logto`). Supported. Needed for the mobile app. You host Logto yourself. |
| Backend | Python 3.12 and FastAPI | None |
| Frontend | Vite, React, TypeScript, Tailwind CSS | Backend only (`--no-frontend`) |

## Not in scope

- Creating Google OAuth clients. Each app owner creates their own in Google Cloud Console.
- Applying the Render Blueprint or paying for resources.
- A visual brand. The tokens are a neutral start. Use `/impeccable` for a real direction.
- A mobile app for Google sign-in apps. The Expo mobile app works only with Logto today.

## Success

A new user runs the documented commands on a clean computer. The new app's tests pass, the app starts with a local Postgres, and it shows "sign-in is not configured" until Google sign-in is set up. A new user can create the Google client from `docs/google-oauth-walkthrough.md` with no other help.
