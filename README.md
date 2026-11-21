# ghdocs

Build a GitHub repository's plain Markdown `docs/` directory as a Sphinx site — no `conf.py`, no changes to the target repository.

```bash
uv run https://github.com/MtkN1/animated-sniffle/raw/refs/heads/main/ghdocs.py acme/foo docs/
```

The HTML is written to `./_build/html`.

## Options

| Argument | Default | Description |
|---|---|---|
| `OWNER/REPO` | (required) | GitHub repository |
| `BASE_PATH` | `docs` | Docs directory (`.` for the repository root) |
| `-o, --outdir` | `_build/html` | Output directory |
| `-b, --builder` | `html` | Sphinx builder |
| `--ref` | default branch | Branch or tag |
| `--source DIR` | — | Use a local checkout instead of cloning |
| `-W` | — | Treat warnings as errors |

## What it handles

- `[x](page.md)`, `../page.md`, `page.md#anchor` → `.html` links (MyST-Parser)
- `[x](dir/)` → `dir/README.html` (or an auto-generated index when the directory has no README)
- Links outside the base path (source code, `../README.md`, …) → `https://github.com/OWNER/REPO/blob/REF/...`
- Non-Markdown files inside the base path are copied as-is and linked directly
- Navigation (toctree) generated from the directory tree, using `README.md` / `index.md` as section pages
- Pages without a heading get their file name as title
- GFM: tables, task lists, strikethrough, autolinks, raw HTML, `> [!NOTE]` alerts, ` ```mermaid `

Theme: [Shibuya](https://shibuya.lepture.com/) (fixed), with GitHub links ("Edit this page", repository card) pointing back to the source repository.

Requires `git` (private repositories work with your git credentials).
