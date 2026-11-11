# Mirror of templates/mini-app/template/{{ app_name }}/app/cli/output.py.jinja — keep in sync.
# When editing one, edit the other. Re-extract to shared package in v2 (see R16).
"""Output rendering for the ``agentic-core`` CLI — pretty (default) or JSON.

Two output modes, enforced by the global ``--json`` flag on the root Typer
callback:

- **Pretty mode** (default): Rich panels/tables on a stderr-aware console.
  Human-readable; non-zero exit codes still print structured Rich panels.
- **JSON mode**: a single JSON object on stdout, nothing else.

The strict stdout discipline (R2 of the spec) is *load-bearing*: agent callers
read stdout as one JSON object and fail closed on extra lines. So:

- All Rich output, log records, progress bars, warnings → stderr.
- ``render_json(...)`` writes one line of JSON to ``sys.stdout`` and nothing else.
- ``emit_failure(...)`` is the JSON-mode error path; writes the failure envelope
  to stdout, then the caller exits non-zero.

Callers MUST NOT use ``print()`` or ``typer.echo()`` directly in JSON mode —
those default to stdout and would corrupt the contract. Use ``console.print(...)``
for human output (always stderr-bound) and ``render_json(...)`` / ``emit_failure(...)``
for the single stdout JSON line.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any

from rich.console import Console

# Rich console pinned to stderr — never stdout. This is the single
# enforcement point for R2's "stdout is JSON-only in --json mode" rule.
# In pretty mode, humans see Rich on stderr (still visible interactively).
# In JSON mode, the same console can emit progress/warnings without polluting
# stdout. Subcommands import THIS console; do not create new Console()s
# elsewhere unless you bind them to a buffer.
#
# Important: use ``stderr=True`` instead of ``file=sys.stderr`` so the
# console picks up the *current* ``sys.stderr`` at write time. This matters
# for the Typer ``CliRunner``, which redirects ``sys.stderr`` to a capture
# buffer per invocation — binding the file at import time would write to
# the original fd and bypass capture.
console: Console = Console(stderr=True, highlight=False)


def render_json(data: Any) -> None:
    """Write ``data`` as a single-line JSON object to stdout.

    Exactly one ``\\n``-terminated line is written. No Rich formatting,
    no ANSI, no human-readable wrapper. ``ensure_ascii=False`` so downstream
    ``jq`` / agents see real UTF-8 rather than ``\\uXXXX`` escapes.
    """
    line = _json.dumps(data, ensure_ascii=False, default=str)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def emit_failure(
    *,
    app_slug: str | None = None,
    checkpoint: str | None = None,
    exit_code: int = 3,
    error_code: str = "UNKNOWN",
    error_message: str = "",
) -> None:
    """Write the R3 failure envelope to stdout (JSON mode).

    Caller is responsible for the non-zero ``sys.exit(exit_code)`` after this
    returns — this function only renders. Schema mirrors the spec §R3 shape:

        {
          "schema_version": 1,
          "status": "failed",
          "app_slug": "...",
          "checkpoint": "...",
          "exit_code": 3,
          "error_code": "...",
          "error_message": "..."
        }
    """
    envelope: dict[str, Any] = {
        "schema_version": 1,
        "status": "failed",
        "app_slug": app_slug,
        "checkpoint": checkpoint,
        "exit_code": exit_code,
        "error_code": error_code,
        "error_message": error_message,
    }
    render_json(envelope)
