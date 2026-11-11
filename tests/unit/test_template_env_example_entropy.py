"""Invariant 8 entropy gate: rendered ``.env.example`` ships only placeholders.

The template's ``.env.example.jinja`` MUST contain no high-entropy strings.
Any such string is presumed-secret (real API keys, hashes, JWTs all match
the pattern). Placeholders like ``__SET_IN_RAILWAY__`` and
``__GENERATED_BY_SCAFFOLD__`` are short enough to stay under the threshold.

Regex per spec §A1.7 (tightened in Task #6 retry — hyphen removed so real
Railway UUIDs and ISO dates segment naturally into <32-char runs):

    /[A-Za-z0-9+/=_]{32,}/

This test renders all three variants. Today only ``full`` ships in
``copier.yml``; ``lightweight-api`` and ``static-dashboard`` are added by
Task #15. We test ``full`` end-to-end; the other two are parameterized via
opt-out flags (``with_auth=False`` / ``with_db=False``) so the matrix is
covered as soon as #15 wires the variant prompt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from copier import run_copy


REPO_ROOT = Path(__file__).parent.parent.parent
TEMPLATE_DIR = REPO_ROOT / "templates" / "mini-app"

# Spec §A1.7 (R6 retry) — hyphen excluded so UUIDs/ISO dates don't trip.
ENTROPY_RE = re.compile(r"[A-Za-z0-9+/=_]{32,}")


def _render(tmp_path: Path, *, app_name: str, **overrides) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    data = {
        "app_name": app_name,
        "app_description": "entropy test",
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
    rendered = out / app_name
    assert rendered.exists(), f"expected rendered app at {rendered}"
    return rendered


# Each tuple = (variant-slug, extra Copier data dict). Today's template only
# has a ``full`` variant; the variant flag flows through anyway so the test
# is forward-compatible with #15 adding the other two.
VARIANTS = [
    ("full", {}),
    ("lightweight-api", {"variant": "lightweight-api"}),
    ("static-dashboard", {"variant": "static-dashboard"}),
]


class TestEnvExampleEntropy:
    """Generated .env.example contains zero high-entropy strings."""

    @pytest.mark.parametrize("variant_name,extra", VARIANTS)
    def test_no_high_entropy_strings(
        self, tmp_path: Path, variant_name: str, extra: dict
    ):
        rendered = _render(
            tmp_path,
            app_name=f"entropy-{variant_name}",
            **extra,
        )
        env_example = rendered / ".env.example"
        assert env_example.exists(), (
            f"variant {variant_name!r} must ship a rendered .env.example "
            f"(expected at {env_example})"
        )
        text = env_example.read_text()
        matches = ENTROPY_RE.findall(text)
        assert matches == [], (
            f"variant {variant_name!r}: .env.example contains "
            f"{len(matches)} high-entropy string(s) — presumed secrets. "
            f"Use placeholders (__SET_IN_RAILWAY__, "
            f"__GENERATED_BY_SCAFFOLD__, __YOUR_VALUE_HERE__, or empty "
            f"`KEY=`). Matches: {matches!r}\n\n"
            f"Full file:\n{text}"
        )

    @pytest.mark.parametrize("variant_name,extra", VARIANTS)
    def test_no_management_api_references(
        self, tmp_path: Path, variant_name: str, extra: dict
    ):
        # Invariant 4: the admin client never reaches a generated app's
        # runtime config. .env.example especially must not advertise the
        # variable names, lest an operator paste real credentials into it.
        rendered = _render(
            tmp_path,
            app_name=f"mgmt-{variant_name}",
            **extra,
        )
        env_example = rendered / ".env.example"
        text = env_example.read_text()
        assert "LOGTO_MANAGEMENT_API" not in text, (
            f"variant {variant_name!r}: .env.example must NOT reference "
            f"LOGTO_MANAGEMENT_API* (Invariant 4). Got:\n{text}"
        )
