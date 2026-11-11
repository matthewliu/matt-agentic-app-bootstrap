"""MCP server for this app (stdio). See server.py."""

import sys


def main() -> None:
    """Console-script entry point. Explains a missing `mcp` extra."""
    try:
        from app.mcp_server.server import main as serve
    except ImportError as exc:
        if exc.name and exc.name.split(".")[0] == "mcp":
            print(
                "error (misconfigured): the MCP server needs the mcp extra; "
                "run: pip install -e '.[mcp]'",
                file=sys.stderr,
            )
            raise SystemExit(5) from None
        raise
    serve()
