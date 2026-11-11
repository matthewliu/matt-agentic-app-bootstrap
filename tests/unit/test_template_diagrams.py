"""Diagrams: sources, copies, image links, and no Mermaid.

Each diagram is an `.html` source plus an exported `.svg` in `docs/diagrams/`.
Some also ship in generated apps. These checks keep the two copies the same
and make sure every Markdown image link points at a file that exists.
"""

import re
from pathlib import Path

import pytest

from tests.conftest import TEMPLATE_DIR

REPO = Path(__file__).resolve().parents[2]
DOCS_DIAGRAMS = REPO / "docs" / "diagrams"
TEMPLATE_DIAGRAMS = next((TEMPLATE_DIR / "template" / "{{ app_name }}").glob("*docs*")) / "diagrams"
IMAGE_LINK = re.compile(r"!\[[^\]]*\]\(([^)\s]+\.svg)\)")

SHIPPED = {
    "one-api-three-doors": "{% if include_frontend and include_cli and include_backend %}",
    "auth-decision": "{% if auth_provider == 'google' and include_backend %}",
    "data-model": "{% if include_db %}",
    "local-dev-setup": "{% if include_frontend and include_db %}",
}


def _markdown_files():
    return [p for p in REPO.glob("*.md")] + list((REPO / "docs").rglob("*.md"))


def test_every_svg_has_an_html_source():
    for svg in DOCS_DIAGRAMS.glob("*.svg"):
        assert svg.with_suffix(".html").is_file(), f"{svg.name} has no .html source"


@pytest.mark.parametrize("slug", sorted(SHIPPED))
def test_template_copies_match_the_docs(slug):
    for ext in ("svg", "html"):
        template_copy = TEMPLATE_DIAGRAMS / f"{SHIPPED[slug]}{slug}.{ext}{{% endif %}}"
        assert template_copy.is_file(), template_copy.name
        assert template_copy.read_bytes() == (DOCS_DIAGRAMS / f"{slug}.{ext}").read_bytes(), (
            f"{slug}.{ext} differs between docs/diagrams and the template; copy it again"
        )


def test_repo_markdown_image_links_resolve():
    for md in _markdown_files():
        for target in IMAGE_LINK.findall(md.read_text()):
            assert (md.parent / target).resolve().is_file(), f"{md.relative_to(REPO)}: {target}"


def test_repo_docs_use_no_mermaid():
    for md in _markdown_files():
        assert "```mermaid" not in md.read_text(), f"{md.relative_to(REPO)} uses Mermaid"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            {"include_frontend": True, "auth_provider": "google", "deploy_target": "render"},
            {"one-api-three-doors", "auth-decision", "data-model", "local-dev-setup"},
        ),
        ({"include_frontend": False, "auth_provider": "google"}, {"auth-decision", "data-model"}),
        ({"include_frontend": True, "auth_provider": "logto"}, {
            "one-api-three-doors", "data-model", "local-dev-setup",
        }),
        ({"include_frontend": False, "with_db": False, "variant": "lightweight-api"}, set()),
    ],
)
def test_generated_app_gets_the_right_diagrams_and_links(copier_copy, data, expected):
    app = copier_copy(app_name="diag-app", include_mobile=False, **data)
    diagrams = app / "docs" / "diagrams"
    shipped = {p.stem for p in diagrams.glob("*.svg")} if diagrams.exists() else set()
    assert shipped == expected
    if not expected:
        assert not (app / "docs").exists(), "an app with no diagrams must not get an empty docs/"
    readme = (app / "README.md").read_text()
    linked = {Path(t).stem for t in IMAGE_LINK.findall(readme)}
    assert linked == expected
