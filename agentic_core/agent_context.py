# Mirror of templates/mini-app/template/{{ app_name }}/app/cli/
# agent_context.py.jinja — keep in sync.
# When editing the ``generate`` / ``_command_info`` / ``_split_params`` / ``_option_flag``
# implementation here, edit the template's twin (and vice versa). The module-level
# constants ``AUTH_INFO`` and ``ERROR_CODES`` legitimately differ between the two
# sides because the auth contract is inherently per-tool — ``agentic-core`` is the
# bootstrap admin CLI, generated apps are per-app Logto M2M. Re-extract to a
# shared package in v2 (see R16).
"""Agent-context introspection — emit the v1 envelope from a Typer app.

Coding agents read ``agentic-core agent-context --json`` once per session to
discover every command, arg, option, and exit-code contract — without
hard-coding command names in agent prompts.

Schema (envelope shape downstream consumers depend on)::

    {
      "schema_version": 1,
      "tool": "agentic-core",
      "version": "<__version__>",
      "cli": {"name": "...", "version": "...", "description": "..."},
      "auth": {...},
      "commands": [
        {
          "name": "<group> <command>",
          "description": "<one-line help>",
          "args": [...],
          "options": [...],
          "examples": []
        },
        ...
      ],
      "error_codes": [
        {"code": 0, "meaning": "Success"},
        ...
      ]
    }

Per spec §B2, exit codes 0-5 are the contract every CLI advertises. Walking
``app.registered_commands`` (top-level) and ``app.registered_groups``
(sub-Typers, one level deep) mirrors the per-app template's pattern.
"""

from __future__ import annotations

import inspect
from typing import Any

import typer


# Fixed exit-code contract — keep aligned with spec §B2 / R4
# (0 success, 1 user, 2 auth, 3 server/app, 4 network, 5 misconfig, 6 rate-limit).
# Code 6 is agentic-core-specific (Logto 429 → ``LOGTO_429_RATE_LIMITED``); the
# per-app template uses 0-5 only because generated apps don't make outbound
# rate-limited calls. Keeping it here documents the full surface agents see
# when calling ``create-app`` and seeing a 6 in their wrapper.
ERROR_CODES: list[dict[str, Any]] = [
    {"code": 0, "meaning": "Success"},
    {"code": 1, "meaning": "User error / invalid input"},
    {"code": 2, "meaning": "Authentication failed"},
    {"code": 3, "meaning": "Application or server error"},
    {"code": 4, "meaning": "Network error"},
    {"code": 5, "meaning": "Misconfiguration"},
    {"code": 6, "meaning": "Rate limited (e.g. Logto 429 — retry with backoff)"},
]


# CQ-002 slug glossary: documents the three string shapes a slug morphs into
# inside the Copier template + envelope. Agents otherwise have to reverse-engineer
# the convention from looking at generated files. ``slug`` is the raw input,
# ``APP_UPPER`` is the env-var prefix (used in ``.env.example`` and SQLAlchemy
# config), and ``App-Name-Title`` is the human-readable form used in README
# titles and Logto application display names.
SLUG_GLOSSARY: dict[str, str] = {
    "slug": (
        "Raw user-supplied identifier (e.g. ``my-dashboard``); lowercase, "
        "matches ``^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$``. Used as the directory "
        "name, GitHub repo name, Railway project name, and Logto application "
        "name."
    ),
    "APP_UPPER": (
        "Underscored, uppercased form (e.g. ``MY_DASHBOARD``). Used as the "
        "env-var prefix in ``.env.example`` and the SQLAlchemy/pytest config "
        "namespace inside the generated app."
    ),
    "App-Name-Title": (
        "Title-cased, hyphen-preserving form (e.g. ``Yc-Dashboard``). Used "
        "in generated README headings and Logto application display name."
    ),
}


# R1 passphrase transport contract: agents read this to know how to hand the
# keystore passphrase to ``agentic-core create-app``. Two paths are supported,
# and the env-var path was explicitly removed in the spec review because env
# is readable via ``/proc/<pid>/environ`` for the scaffold's lifetime.
PASSPHRASE_TRANSPORT_INFO: str = (
    "stdin (the interactive path) OR --passphrase-fd <N> (an agent caller's "
    "pipe path). Env-var transport is intentionally NOT supported (R1) — env "
    "is readable via /proc/<pid>/environ for the scaffold's lifetime."
)


# R7 keystore path precedence: this is the order the CLI walks to locate
# the admin keystore. Documenting it here lets agents understand which env
# var to set when they want to point at a sandbox keystore.
KEYSTORE_PATH_RESOLUTION: list[str] = [
    "--admin-keystore <PATH>  (CLI flag, highest precedence)",
    "$AGENTIC_CORE_ADMIN_KEYSTORE  (env var)",
    "~/.agentic-core/admin.keystore.json  (local default, lowest precedence)",
]


# Auth metadata for ``agentic-core`` itself. Unlike the per-app template (which
# describes per-app Logto M2M + actor-sub header), ``agentic-core`` is the
# *bootstrap admin* CLI: it uses the ``agentic-core-admin`` Logto M2M
# application against the Logto Management API to create/delete per-app Logto
# applications, and decrypts long-lived operator credentials from the local
# eth-account v3 keystore. Hand-curated (not derived from code) so agents
# can check it against the code for drift.
AUTH_INFO: dict[str, Any] = {
    "description": (
        "Bootstrap admin. ``agentic-core`` mints a Logto Management API token "
        "with the ``agentic-core-admin`` M2M application (credentials in "
        "``LOGTO_M2M_APP_ID``/``LOGTO_M2M_APP_SECRET`` against "
        "``LOGTO_ENDPOINT``) to provision per-app Logto applications, API "
        "resources, scopes, and roles. Operator-scoped credentials (Railway "
        "tokens, GitHub PAT) are encrypted at rest under the eth-account v3 "
        "admin keystore (resolved per R7 — see ``keystore_path_resolution``). "
        "The keystore passphrase is read per R1 — see "
        "``passphrase_transport``. No per-app actor-sub header applies — "
        "``agentic-core`` does not call generated apps' runtime APIs."
    ),
    "audit_log": None,
    "passphrase_transport": PASSPHRASE_TRANSPORT_INFO,
    "keystore_path_resolution": list(KEYSTORE_PATH_RESOLUTION),
    "required_env_vars": [
        "LOGTO_ENDPOINT",
        "LOGTO_M2M_APP_ID",
        "LOGTO_M2M_APP_SECRET",
        # Note: AGENTIC_CORE_ADMIN_KEYSTORE (keystore path) is optional; falls
        # back to ~/.agentic-core/admin.keystore.json per R7.
        # No passphrase env var (R1 — env is leakable via /proc/<pid>/environ).
    ],
}


def generate(app: typer.Typer, version: str) -> dict[str, Any]:
    """Introspect ``app`` and return a v1 agent-context dict.

    Walks ``app.registered_commands`` (top-level) and ``app.registered_groups``
    (sub-Typers, one level deep — matches the per-app template's tree shape).
    For each command, separates positional args (no default) from options
    (with default or boolean flag).
    """
    description = _app_description(app)
    commands: list[dict[str, Any]] = []

    for cmd in getattr(app, "registered_commands", []) or []:
        commands.append(_command_info(cmd, prefix=""))

    for group in getattr(app, "registered_groups", []) or []:
        group_name = group.name or _callback_name(group.typer_instance)
        sub_app = group.typer_instance
        for cmd in getattr(sub_app, "registered_commands", []) or []:
            commands.append(_command_info(cmd, prefix=group_name))

    return {
        "schema_version": 1,
        "tool": "agentic-core",
        "version": version,
        "cli": {
            "name": app.info.name or "agentic-core",
            "version": version,
            "description": description,
        },
        "auth": dict(AUTH_INFO),
        "commands": commands,
        "error_codes": list(ERROR_CODES),
        "slug_glossary": dict(SLUG_GLOSSARY),
    }


def _app_description(app: typer.Typer) -> str:
    info = getattr(app, "info", None)
    if info is not None:
        help_text = getattr(info, "help", None)
        if help_text:
            return help_text
    return ""


def _callback_name(sub_app: typer.Typer) -> str:
    info = getattr(sub_app, "info", None)
    if info is not None and getattr(info, "name", None):
        return info.name
    return ""


def _command_info(cmd: Any, prefix: str = "") -> dict[str, Any]:
    """Return one command's introspected schema entry."""
    callback = getattr(cmd, "callback", None)
    base_name = cmd.name or (callback.__name__ if callback else "")
    if not cmd.name and base_name.endswith("_"):
        base_name = base_name.rstrip("_")

    full_name = f"{prefix} {base_name}".strip() if prefix else base_name

    description = (cmd.help or "").strip()
    if not description and callback and callback.__doc__:
        description = inspect.cleandoc(callback.__doc__).splitlines()[0].strip()

    args, options = _split_params(callback)

    return {
        "name": full_name,
        "description": description,
        "args": args,
        "options": options,
        "examples": [],
    }


def _split_params(callback: Any) -> tuple[list[str], list[str]]:
    """Separate positional args from --options for a Typer callback."""
    args: list[str] = []
    options: list[str] = []
    if callback is None:
        return args, options

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return args, options

    for name, param in signature.parameters.items():
        if param.annotation is typer.Context:
            continue
        if name in {"ctx", "context"} and param.annotation is inspect.Parameter.empty:
            continue

        default = param.default
        default_cls = type(default).__name__ if default is not inspect.Parameter.empty else ""

        if default_cls == "ArgumentInfo":
            args.append(name)
        elif default_cls == "OptionInfo":
            options.append(_option_flag(name, default))
        elif default is inspect.Parameter.empty:
            args.append(name)
        else:
            options.append(f"--{name.replace('_', '-')}")

    return args, options


def _option_flag(name: str, option_info: Any) -> str:
    """Pick a representative flag string for a typer.Option."""
    decls = getattr(option_info, "param_decls", None) or ()
    for decl in decls:
        if isinstance(decl, str) and decl.startswith("--"):
            return decl
    return f"--{name.replace('_', '-')}"
