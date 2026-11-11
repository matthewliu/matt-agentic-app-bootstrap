# Diagrams

Each diagram has two files:

- `<name>.html`: the source. Open it in a browser to see the diagram. Edit it to change the diagram.
- `<name>.svg`: the image that the Markdown guides show. Make it again from the `.html` file after each change.

The diagrams are made with the `diagram-design` agent skill. Do not use Mermaid in these docs.

## Style

`diagram-style.md` is the starter style. It matches the colors of a generated app (`frontend/src/design/tokens.ts`): zinc neutrals and an indigo accent. It uses system fonts, because GitHub does not load web fonts inside an SVG image. You can change it.

To use the style with the diagram-design skill:

1. Copy `diagram-style.md` to `~/.diagram-design/profiles/agentic-app.md`.
2. Keep the `.diagram-design` file at the repo root. It contains `profile: agentic-app`, so the skill uses this style in this repo.

## Make the SVG again

From the diagram-design skill folder:

```bash
python3 scripts/self_check.py --offline <repo>/docs/diagrams/<name>.html
python3 scripts/verify_geometry.py <repo>/docs/diagrams/<name>.html
python3 scripts/export_svg.py <repo>/docs/diagrams/<name>.html <repo>/docs/diagrams/<name>.svg --system-fonts
```

## Copies in the app template

Some diagrams also ship in each generated app, in `templates/mini-app/template/{{ app_name }}/docs/diagrams/`. When you change one of these diagrams here, copy both files there too. `tests/unit/test_template_diagrams.py` fails when the two copies differ.

| Diagram | Used in |
| --- | --- |
| `one-api-three-doors` | README, architecture, beginner guide, generated apps with a web app |
| `create-app-flow` | README, architecture |
| `google-sign-in-sequence` | Google sign-in walkthrough, Google auth design guide |
| `google-console-setup` | Google sign-in walkthrough |
| `auth-decision` | architecture, generated Google apps |
| `data-model` | architecture, beginner guide, generated apps with a database |
| `choosing-options` | choosing-options |
| `local-dev-setup` | README, beginner guide, generated apps with a web app and a database |
