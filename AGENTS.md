# Agentic Bootstrap (`agentic-core`): agent entry point

`agentic-core` is a Python CLI and a Copier template that create new FastAPI apps with Google sign-in, a React frontend, a CLI, an MCP server, and a Render Blueprint.

**Read [CLAUDE.md](CLAUDE.md) first.** It is the full project guide for every coding agent: verified commands, defaults, security boundaries, and the rules a new agent would not guess. This file does not repeat it, so the two files cannot disagree.

## Shortest path

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # python3 must be 3.12+; else use python3.12 or uv
.venv/bin/python -m pytest tests/ -x -q
.venv/bin/ruff check .
```

## Before you change code

- Template auth changes must keep Logto output byte-identical. See CLAUDE.md, "Rules a new agent would not guess".
- Auth, data access, and `render.yaml` are security boundaries. Say so in the PR when you change one.
- Read `.claude/memory/MEMORY.md` and `docs/solutions/` when you work in an area they list.
- Many users are new to coding. To explain a concept, use `docs/beginner-guide.md`. For Google sign-in setup, use `docs/google-oauth-walkthrough.md`.
- Every generated app must serve people and agents with one set of rules. See `docs/architecture.md`.
- Make diagrams with the `diagram-design` skill, never Mermaid. See `docs/diagrams/README.md`.
- Plans, reviews, and research from past changes are not in the current files. Do not restore them as instructions.
