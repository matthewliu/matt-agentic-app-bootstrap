"""Pytest config for integration tests — registers --run-slow + slow marker.

The slow tests in this directory (notably the generated-app CLI subprocess
install in ``test_generated_app_cli.py``) are marked ``@pytest.mark.slow``.
They currently run **unconditionally** so CI and local pytest invocations
get the same coverage by default. The ``--run-slow`` flag is accepted for
forward compatibility with task #18's scoped verification command and to
let future authors gate even slower tests behind it.

If you want to skip slow tests locally: ``pytest -m "not slow"``.
If you want ONLY the slow tests: ``pytest -m slow``.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register ``--run-slow``. Currently informational (tests run anyway)."""
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help=(
            "Force-run @pytest.mark.slow tests. Currently a no-op — slow "
            "tests run by default. Reserved for future opt-in gating."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``slow`` marker so pytest stops emitting unknown-marker warnings."""
    config.addinivalue_line(
        "markers",
        "slow: integration test that takes >1s (e.g. subprocess pip install).",
    )
