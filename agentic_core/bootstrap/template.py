"""Copier template wrapper.

Per PERF-004 (spec §R19): Copier is invoked in-process via
:func:`copier.run_copy`, **never** via ``subprocess.run(['copier', ...])``.
The subprocess path takes ~600ms of Python startup per call; the in-process
path is sub-100ms and shares the test runner's Python interpreter (so test
fixtures don't have to re-resolve the venv binary).

This module is the single seam between ``agentic-core create-app`` and
Copier. The orchestrator (#8) calls :func:`render_template`; the variant
name + opt-out flags flow through Copier's ``data`` dict so the template's
Jinja conditionals can branch on them.

Variant note: today only ``full`` exists in ``copier.yml``;
``static-dashboard`` and ``lightweight-api`` land with Task #15. This
wrapper accepts all three names so #15 can plug in without touching the
wrapper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from copier import run_copy


# Variants the wrapper accepts. Keep in sync with spec §A3 and copier.yml's
# eventual variant prompt (added by Task #15). Listing them here lets the
# wrapper fail fast on typos rather than handing a bogus name to Copier and
# rendering the wrong tree.
VALID_VARIANTS = frozenset({"full", "lightweight-api", "static-dashboard"})


def render_template(
    template_dir: Path,
    output_dir: Path,
    data: dict[str, Any],
    variant: str = "full",
) -> None:
    """Render the mini-app Copier template into ``output_dir``.

    Args:
        template_dir: Path to ``templates/mini-app/`` (where ``copier.yml``
            lives).
        output_dir: Destination directory. Copier renders
            ``template/{{ app_name }}/`` into ``output_dir/{{ app_name }}/``.
        data: Copier variables matching keys declared in ``copier.yml``
            (currently ``app_name``, ``app_description``,
            ``python_package_name``, ``include_frontend``, ``include_mobile``,
            ``github_user``, ``platform_domain``, ``logto_endpoint``). The
            ``variant`` arg is merged into this dict under the ``variant``
            key so the template can branch on it via Jinja. Future opt-out
            flags (``with_auth``, ``with_db``) land with Task #15 when the
            ``lightweight-api`` and ``static-dashboard`` variants ship.
        variant: ``full`` (default) | ``lightweight-api`` |
            ``static-dashboard``. Raises ``ValueError`` for unknown names.

    PERF-004: invokes :func:`copier.run_copy` directly. Never shells out.

    Tasks (``_tasks`` in copier.yml — venv create, pip install, git init)
    are skipped here. The orchestrator owns those side effects so they can
    be sequenced with the rest of the scaffold (e.g. delayed until after
    GitHub repo push) and so tests can render without ~30s of npm/pip
    install per test.
    """
    if variant not in VALID_VARIANTS:
        raise ValueError(
            f"unknown variant {variant!r}; must be one of "
            f"{sorted(VALID_VARIANTS)}"
        )

    # Copy so we never mutate the caller's dict.
    merged_data: dict[str, Any] = dict(data)
    merged_data["variant"] = variant

    run_copy(
        src_path=str(template_dir),
        dst_path=output_dir,
        data=merged_data,
        defaults=True,
        # `unsafe=True` lets templates use Jinja extensions and `_tasks`.
        # The template is repo-local (not fetched from a remote URL) so the
        # standard "trust" concern doesn't apply — see Copier docs on
        # `unsafe`. Mirrors the existing tests/conftest.py copier_copy
        # fixture's `--trust` flag.
        unsafe=True,
        # `skip_tasks=True` mirrors conftest's `--skip-tasks`. The
        # orchestrator (#8) runs venv/pip/git itself in the right order.
        skip_tasks=True,
        # Quiet output: in `--json` mode we forbid stdout chatter from
        # subprocesses anyway, but quiet=True also avoids polluting stderr
        # with Copier progress bars during tests.
        quiet=True,
        overwrite=True,
    )
