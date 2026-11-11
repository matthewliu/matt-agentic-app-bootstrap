"""agentic_core — bootstrap CLI for scaffolding mini-apps.

The ``agentic-core`` console script (declared in ``pyproject.toml``) wires up to
:mod:`agentic_core.cli`. Sub-modules under ``agentic_core.bootstrap`` land in
later tasks; this scaffold ships the Typer entry point, the Rich/JSON output
shim, and the v1 ``agent-context`` envelope stub so downstream tasks
have a stable surface to extend.
"""

__version__ = "0.1.0"
