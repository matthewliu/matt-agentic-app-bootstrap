"""Registry lister for ``agentic-core list-apps`` (spec §R18).

Pure read-only scan of sibling app directories: walks the parent dir of the
agentic-core checkout, finds each subdirectory containing a
``.bootstrap-state.json`` file, parses it, and emits a structured registry.

§R18 — NO live status checks
----------------------------
This module deliberately does NOT call out to Logto, GitHub, or Railway to
verify resources still exist. The state file is the registry of record;
keeping ``list-apps`` offline-safe means:

  - Agents can call it repeatedly without burning API quota or rate limits.
  - It works on planes / offline laptops / sandboxed CI.
  - There is exactly one truth (the on-disk state) — no "we said X but the
    API says Y" reconciliation problem.

Operators (or a future ``agentic-core verify`` command) own live verification.

Robustness
----------
Corrupt state files are skipped with a stderr warning. The registry is
best-effort: one malformed file should not deprive the operator of the
visibility into the healthy apps. (Compare with ``read_state`` in
:mod:`agentic_core.bootstrap.state`, which raises ``StateFileCorrupt`` because
the orchestrator MUST fail closed when resuming — different invariant.)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from agentic_core.bootstrap import state as state_mod
from agentic_core.output_schema import ListAppsEntry, ListAppsEnvelope


# Repo-relative resolver: agentic-core/ lives as a sibling alongside the apps,
# so the parent of our package directory is the directory to scan. Wrapped in
# a function so tests can monkeypatch a tmp_path target without touching the
# real filesystem.
def _resolve_sibling_root(override: Path | None = None) -> Path:
    """Default scan root: the parent of the agentic-core checkout.

    ``override`` (typically from ``--root-dir``) wins when supplied. Otherwise
    we walk up two levels from this file: ``agentic_core/list_apps.py`` →
    ``agentic_core/`` → repo root → parent (siblings live here).
    """
    if override is not None:
        return Path(override).expanduser().resolve()
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root.parent


def _build_block(
    resource_ids: dict[str, str], prefix: str
) -> dict[str, Any] | None:
    """Pluck ``<prefix>_*`` resource IDs into a sub-block, stripping the prefix.

    Returns ``None`` when no keys match — preserves the schema contract
    that an absent surface (``--no-auth``, ``--no-github``, ``--no-railway``)
    shows up as ``null`` rather than an empty dict.

    Used identically for the ``logto`` / ``github`` / ``railway`` sub-blocks;
    factored to keep the projection rule in one place.
    """
    needle = f"{prefix}_"
    block: dict[str, Any] = {
        key[len(needle):]: val
        for key, val in resource_ids.items()
        if key.startswith(needle)
    }
    return block or None


def _entry_from_state(
    state: state_mod.BootstrapState, state_path: Path
) -> ListAppsEntry:
    """Project a ``BootstrapState`` into a public ``ListAppsEntry``."""
    return ListAppsEntry(
        slug=state.app_slug,
        variant=state.variant,
        scaffolded_by=state.scaffolded_by,
        logto=_build_block(state.resource_ids, "logto"),
        github=_build_block(state.resource_ids, "github"),
        railway=_build_block(state.resource_ids, "railway"),
        created_at=state.created_at,
        state_file=str(state_path),
    )


def scan_registry(root: Path | None = None) -> ListAppsEnvelope:
    """Scan ``root`` for sibling app directories and return the envelope.

    Missing ``root`` (directory does not exist) yields an empty list and a
    success envelope — a fresh setup with no apps yet is the steady state
    on a freshly-cloned machine, not an error.
    """
    target = _resolve_sibling_root(root) if root is None else Path(root)
    apps: list[ListAppsEntry] = []

    if not target.exists() or not target.is_dir():
        return ListAppsEnvelope(apps=[], scanned_dir=str(target))

    # Sorted for deterministic output — agents diffing two snapshots benefit
    # from stable ordering more than they care about disk inode order.
    for child in sorted(target.iterdir()):
        if not child.is_dir():
            continue
        state_path = child / ".bootstrap-state.json"
        if not state_path.exists():
            continue
        try:
            state = state_mod.read_state(state_path)
        except state_mod.StateFileCorrupt as exc:
            # Best-effort registry: surface a warning on stderr (R2 keeps
            # stdout JSON-only), then skip this sibling.
            print(
                f"warning: skipping {state_path}: {exc}",
                file=sys.stderr,
            )
            continue
        except FileNotFoundError:
            # Lost a race with concurrent deletion — ignore silently.
            continue

        apps.append(_entry_from_state(state, state_path))

    return ListAppsEnvelope(apps=apps, scanned_dir=str(target))
