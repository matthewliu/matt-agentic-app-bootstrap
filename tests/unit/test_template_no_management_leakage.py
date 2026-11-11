"""R15 dummy-secret-injection test: catch structural leak of any Copier
variable named like an admin credential into a rendered file.

The earlier brittle-grep approach (search rendered files for the literal
string ``LOGTO_MANAGEMENT_API``) misses the real failure mode: a future
template author copy-pastes a Copier variable like
``{{ logto_management_api_secret }}`` into a rendered file. Today's
copier.yml doesn't *declare* such a variable, so the render silently
substitutes empty string and the entropy gate (Task #13 sibling test)
passes — even though structurally the template is now wired to leak admin
credentials the moment that variable is added to copier.yml.

This test injects a deliberately silly, high-entropy alphanumeric canary
value into the Copier ``data`` dict under variable names that resemble
admin credentials (``logto_management_api_secret``,
``admin_m2m_secret``, etc.). If the template references ANY of those
variables in ANY rendered file, the canary will appear in the rendered
output and we fail.

The canary deliberately matches ``[A-Za-z0-9+/=_]{32,}`` (the entropy
regex, per Task #6 retry — no hyphens) so a developer running the entropy
gate on this test's output would also catch the leak. Belt + suspenders.

Spec ref: §R15, §Invariant 4.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from copier import run_copy


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"

# 34 chars, alphanumeric only — deliberately silly so it can't collide with
# anything real, and matches the entropy regex if it ever appears in
# rendered output.
DUMMY_SECRET = "XYZABC123DEFGHIJK456LMNOP789QRSTUV"

# Variable names the test will inject. Copier 9.x silently ignores unknown
# keys in ``data`` (they don't appear in the prompt set) — so a leak occurs
# ONLY if the template explicitly references one of these names. Listing
# every name that smells like an admin credential gives wide coverage.
SUSPICIOUS_VAR_NAMES = (
    "logto_management_api_secret",
    "logto_management_api_id",
    "logto_management_api_resource",
    "logto_m2m_app_secret",
    "logto_m2m_app_id",
    "admin_m2m_secret",
    "admin_m2m_id",
    "agentic_core_admin_secret",
    "agentic_core_admin_id",
)


VARIANTS = [
    ("full", {}),
    ("lightweight-api", {"variant": "lightweight-api"}),
    ("static-dashboard", {"variant": "static-dashboard"}),
]


def _render_with_canary(
    tmp_path: Path, *, app_name: str, extra: dict
) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    data: dict = {
        "app_name": app_name,
        "app_description": "leakage test",
        "include_frontend": False,
        "include_mobile": False,
        "github_user": "testuser",
        "logto_endpoint": "https://logto.example.com",
    }
    # Inject canary under every suspicious name. Copier ignores unknowns.
    for name in SUSPICIOUS_VAR_NAMES:
        data[name] = DUMMY_SECRET
    data.update(extra)
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
    rendered = out / app_name
    assert rendered.exists(), f"expected rendered app at {rendered}"
    return rendered


def _walk_files(root: Path):
    """Yield every regular file under ``root``, skipping virtualenvs and
    other generated noise that wouldn't be shipped by the template anyway.
    """
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip common generated dirs (defence-in-depth — shouldn't exist
        # because we skip_tasks=True, but harmless if they do).
        parts = set(path.parts)
        if parts & {".venv", "venv", "node_modules", "__pycache__", ".git"}:
            continue
        yield path


class TestNoManagementLeakage:
    """No suspicious admin-credential variable bleeds into any rendered file."""

    @pytest.mark.parametrize("variant_name,extra", VARIANTS)
    def test_no_canary_in_any_rendered_file(
        self, tmp_path: Path, variant_name: str, extra: dict
    ):
        rendered = _render_with_canary(
            tmp_path,
            app_name=f"leak-{variant_name}",
            extra=extra,
        )

        offenders: list[tuple[Path, int]] = []
        for path in _walk_files(rendered):
            try:
                text = path.read_text()
            except (UnicodeDecodeError, PermissionError):
                # Binary or unreadable — admin secrets wouldn't survive
                # binary encoding intact, so skipping is safe.
                continue
            count = text.count(DUMMY_SECRET)
            if count:
                offenders.append((path.relative_to(rendered), count))

        assert offenders == [], (
            f"variant {variant_name!r}: dummy admin secret leaked into "
            f"{len(offenders)} rendered file(s). The template references a "
            f"Copier variable whose name matches an admin credential "
            f"(one of {sorted(SUSPICIOUS_VAR_NAMES)!r}) — Invariant 4 "
            f"violation. Offending files (path, occurrence-count):\n"
            + "\n".join(f"  {p}: {c}" for p, c in offenders)
        )
