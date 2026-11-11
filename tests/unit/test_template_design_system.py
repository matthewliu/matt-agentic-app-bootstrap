"""Generated apps ship one design-token source, light and dark mode, named
mobile-first breakpoints, and a DESIGN.md that explains them."""

from __future__ import annotations

import pytest


@pytest.fixture
def google_app(copier_copy):
    return copier_copy(
        app_name="dapp",
        include_frontend=True,
        include_mobile=False,
        auth_provider="google",
        deploy_target="render",
    )


def test_tokens_hold_every_design_category(google_app):
    tokens = (google_app / "frontend" / "src" / "design" / "tokens.ts").read_text()
    for name in ("themes", "fonts", "spacing", "radii", "motion", "breakpoints", "layout"):
        assert f"export const {name}" in tokens, name
    assert "light: {" in tokens and "dark: {" in tokens
    for breakpoint in ("phone: 0", "tablet: 640", "desktop: 1024", "wide: 1280"):
        assert breakpoint in tokens, breakpoint


def test_tailwind_config_holds_no_design_values(google_app):
    """Values live in tokens.ts only, so one edit changes the whole app."""
    config = (google_app / "frontend" / "tailwind.config.ts").read_text()
    assert "#" not in config.split("export default", 1)[1]
    assert "Urbanist" not in config
    for prefix in ("tablet:", "desktop:", "wide:"):
        assert f"{prefix} px(breakpoints." in config, prefix


def test_theme_is_applied_before_first_paint(google_app):
    html = (google_app / "frontend" / "index.html").read_text()
    assert '<meta name="color-scheme" content="light dark" />' in html
    assert 'localStorage.getItem("theme")' in html
    assert 'setAttribute("data-theme", theme)' in html
    theme = (google_app / "frontend" / "src" / "theme.ts").read_text()
    assert 'export const STORAGE_KEY = "theme";' in theme


def test_google_header_has_the_theme_toggle(google_app):
    layout = (google_app / "frontend" / "src" / "components" / "Layout.tsx").read_text()
    assert 'import ThemeToggle from "./ThemeToggle";' in layout
    assert "<ThemeToggle />" in layout


def test_design_md_explains_tokens_themes_breakpoints_and_impeccable(google_app):
    design = (google_app / "DESIGN.md").read_text()
    assert design.startswith("# dapp design\n")
    assert "frontend/src/design/tokens.ts" in design
    assert "prefers-color-scheme" in design
    assert "| tablet | 640 px | `tablet:` |" in design
    assert "/impeccable" in design


def test_backend_only_app_has_no_design_md(copier_copy):
    app = copier_copy(app_name="bapp", include_frontend=False, include_mobile=False)
    assert not (app / "DESIGN.md").exists()


def test_claude_md_points_to_design_md(google_app):
    assert "DESIGN.md" in (google_app / "CLAUDE.md").read_text()


def _hex_pairs(tokens: str, theme: str) -> dict[str, str]:
    """Read one theme's color map from tokens.ts (the file is plain data)."""
    import re

    block = tokens.split(f"  {theme}: {{", 1)[1].split("shadows:", 1)[0]
    return dict(re.findall(r'"?([a-z-]+)"?: "(#[0-9a-f]{6})"', block))


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        rgb = [int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_colors_meet_wcag_aa_in_both_themes(google_app, theme):
    """DESIGN.md promises 4.5:1 for text. Check every text and background pair."""
    tokens = (google_app / "frontend" / "src" / "design" / "tokens.ts").read_text()
    c = _hex_pairs(tokens, theme)
    pairs = [
        ("text-primary", "bg-primary"),
        ("text-secondary", "bg-primary"),
        ("text-muted", "bg-primary"),
        ("text-muted", "bg-secondary"),
        ("accent-contrast", "accent"),
        ("accent-contrast", "accent-hover"),
        ("error", "bg-primary"),
    ]
    for fg, bg in pairs:
        ratio = _contrast(c[fg], c[bg])
        assert ratio >= 4.5, f"{theme}: {fg} on {bg} is {ratio:.2f}:1"


def test_google_login_button_uses_the_contrast_token(google_app):
    page = (google_app / "frontend" / "src" / "pages" / "LoginPage.tsx").read_text()
    assert "bg-accent" in page and "text-accent-contrast" in page
    assert "text-white" not in page
