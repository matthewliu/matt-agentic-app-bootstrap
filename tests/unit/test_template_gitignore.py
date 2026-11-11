"""Invariant 8 + R8: rendered ``.gitignore`` shields long-lived secrets.

The template MUST ship a ``.gitignore`` that prevents a generated app from
accidentally committing any of the following:

- ``.env`` / ``.env.local`` — runtime secrets pulled from Railway
- ``*.keystore.json`` — encrypted admin keystore (lives outside the repo,
  but we belt-and-suspenders ignore the glob in case anyone drops one in)
- ``*.bak*`` — any backup file (editors, scripts)
- ``*.last-good`` — atomic-write sidecars from bootstrap state writes
- ``.bootstrap-state.json`` — bootstrap checkpoint (R8) — contains resource
  IDs which, while not secret on their own, are still scaffold-internal

Every variant must render the same patterns, so the test renders ``full``
(the only variant shipping today; static-dashboard and lightweight-api land
with Task #15) and grep on the resulting file.

When the wrapper from Task #6 (``agentic_core.bootstrap.template``) accepts
the unknown variants, we still render via the public ``copier.run_copy``
API directly to avoid coupling this gate to Track A's wrapper internals.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from copier import run_copy


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"

# Per spec §Invariants 8 + R8 — case-sensitive substrings.
REQUIRED_PATTERNS = (
    ".env",
    ".env.local",
    "*.keystore.json",
    "*.bak*",
    "*.last-good",
    ".bootstrap-state.json",
)


def _render(tmp_path: Path, **overrides) -> Path:
    """Render the ``full`` variant; return path to the rendered app dir."""
    out = tmp_path / "out"
    out.mkdir()
    data = {
        "app_name": "gitignore-test",
        "app_description": "test",
        "include_frontend": False,
        "include_mobile": False,
        "github_user": "testuser",
        "logto_endpoint": "https://logto.example.com",
        **overrides,
    }
    run_copy(
        src_path=str(TEMPLATE_DIR),
        dst_path=str(out),
        data=data,
        defaults=True,
        unsafe=True,
        skip_tasks=True,
        quiet=True,
        overwrite=True,
    )
    rendered = out / data["app_name"]
    assert rendered.exists(), f"expected rendered app at {rendered}"
    return rendered


class TestGitignorePatterns:
    """All 6 required patterns appear (case-sensitive) in the rendered file."""

    @pytest.fixture
    def gitignore_text(self, tmp_path: Path) -> str:
        rendered = _render(tmp_path)
        gitignore = rendered / ".gitignore"
        assert gitignore.exists(), (
            f"template must ship a .gitignore (jinja or static) — none found at {gitignore}"
        )
        return gitignore.read_text()

    @pytest.mark.parametrize("pattern", REQUIRED_PATTERNS)
    def test_pattern_present(self, gitignore_text: str, pattern: str):
        # Substring match is sufficient — these are literal gitignore patterns,
        # not regexes, and false-positive collisions (e.g. ".env" matching
        # ".env.local" line) only HELP the test, never harm it.
        assert pattern in gitignore_text, (
            f"rendered .gitignore must contain {pattern!r} "
            f"(Invariant 8 / R8). Got:\n{gitignore_text}"
        )
