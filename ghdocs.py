# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "sphinx>=8",
#     "myst-parser>=4",
#     "linkify-it-py",
#     "sphinxcontrib-mermaid",
#     "shibuya",
# ]
# ///
"""Build a plain GitHub-style Markdown docs directory as a Sphinx site.

Usage:
    uv run ghdocs.py OWNER/REPO [BASE_PATH] [-o OUTDIR] [--ref REF] [-b BUILDER]

Example:
    uv run ghdocs.py acme/foo docs/
"""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from docutils import nodes
from docutils.transforms import Transform
from sphinx import addnodes
from sphinx.application import Sphinx
from sphinx.transforms.post_transforms import SphinxPostTransform
from sphinx.util import logging

logger = logging.getLogger(__name__)

EXT_NAME = "ghdocs_ext"
INDEX_NAMES = ("README", "readme", "Readme", "index")
GENERATED_INDEX = "index"
MD_SUFFIXES = (".md", ".markdown")
ALERTS = {
    "NOTE": nodes.note,
    "TIP": nodes.tip,
    "IMPORTANT": nodes.important,
    "WARNING": nodes.warning,
    "CAUTION": nodes.caution,
}


# ---------------------------------------------------------------------------
# Repository checkout
# ---------------------------------------------------------------------------


def git(*args: str, cwd: Path | None = None) -> str:
    res = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return res.stdout.strip()


def checkout(repo: str, base: str, ref: str | None, dest: Path) -> str:
    """Shallow + sparse clone of ``repo`` into ``dest``. Returns the ref name."""
    url = f"https://github.com/{repo}.git"
    cmd = ["clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse"]
    if ref:
        cmd += ["--branch", ref]
    logger.info(f"cloning {url} ...")
    try:
        git(*cmd, url, str(dest))
        if base:
            git("sparse-checkout", "set", base, cwd=dest)
        else:
            git("sparse-checkout", "disable", cwd=dest)
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: git {' '.join(e.cmd[1:])} failed:\n{e.stderr}")
    if ref:
        return ref
    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)
    return branch if branch != "HEAD" else git("rev-parse", "HEAD", cwd=dest)


def normalize_base(base: str) -> str:
    base = base.replace("\\", "/").strip("/")
    return "" if base in ("", ".") else posixpath.normpath(base)


# ---------------------------------------------------------------------------
# Helpers shared by the Sphinx extension
# ---------------------------------------------------------------------------


def index_of(dirname: str, docs: set[str]) -> str | None:
    for name in INDEX_NAMES:
        doc = posixpath.join(dirname, name) if dirname else name
        if doc in docs:
            return doc
    return None


def toctree_children(index_doc: str, docs: set[str]) -> list[str]:
    """Docs in the same directory + index docs of the direct sub directories."""
    base = posixpath.dirname(index_doc)
    prefix = f"{base}/" if base else ""
    files, subdirs = [], set()
    for doc in sorted(docs):
        if doc == index_doc or not doc.startswith(prefix):
            continue
        rest = doc[len(prefix) :]
        if "/" in rest:
            subdirs.add(prefix + rest.split("/", 1)[0])
        else:
            files.append(doc)
    out = files
    for sd in sorted(subdirs):
        idx = index_of(sd, docs)
        if idx:
            out.append(idx)
    return out


def generate_missing_indexes(srcdir: Path, base: str) -> list[Path]:
    """Create ``index.md`` in every directory (under base) that has Markdown
    somewhere below it but neither README nor index. Returns created files."""
    root = srcdir / base if base else srcdir
    created = []
    dirs = [root, *sorted(p for p in root.rglob("*") if p.is_dir())]
    for d in dirs:
        if any(part.startswith(".") for part in d.relative_to(srcdir).parts):
            continue
        if not any(f.suffix in MD_SUFFIXES for f in d.rglob("*") if f.is_file()):
            continue
        if any((d / f"{n}{s}").is_file() for n in INDEX_NAMES for s in MD_SUFFIXES):
            continue
        title = d.name if d != srcdir else srcdir.name
        (d / f"{GENERATED_INDEX}.md").write_text(f"# {title}\n", encoding="utf-8")
        created.append(d / f"{GENERATED_INDEX}.md")
    return created


def has_heading(text: str) -> bool:
    fence = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            mark = stripped[:3]
            fence = None if fence == mark else (fence or mark)
            continue
        if fence is None and re.match(r"#{1,6}\s", line):
            return True
    return False


# ---------------------------------------------------------------------------
# Sphinx extension
# ---------------------------------------------------------------------------


def on_source_read(app: Sphinx, docname: str, source: list[str]) -> None:
    # Pages without any heading get the file name as title (needed for toctree).
    if not has_heading(source[0]):
        source[0] = f"# {posixpath.basename(docname)}\n\n" + source[0]
    # Directory index pages get a hidden toctree built from the file tree.
    if posixpath.basename(docname) in INDEX_NAMES:
        kids = toctree_children(docname, set(app.env.found_docs))
        if kids:
            entries = "\n".join(f"/{k}" for k in kids)
            source[0] += f"\n\n```{{toctree}}\n:hidden:\n\n{entries}\n```\n"


class GitHubAlerts(Transform):
    """``> [!NOTE]`` style block quotes -> admonitions."""

    default_priority = 200

    def apply(self) -> None:
        for bq in list(self.document.findall(nodes.block_quote)):
            if not bq.children or not isinstance(bq[0], nodes.paragraph):
                continue
            para = bq[0]
            m = re.match(r"\[!(\w+)\]", para.astext())
            if not m or m.group(1).upper() not in ALERTS:
                continue
            first = para[0]
            if isinstance(first, nodes.Text):
                rest = str(first)[m.end() :].lstrip()
                if rest:
                    para.replace(first, nodes.Text(rest))
                else:
                    para.remove(first)
            if not para.children:
                bq.remove(para)
            bq.replace_self(ALERTS[m.group(1).upper()]("", *bq.children))


class LinkResolver(SphinxPostTransform):
    """Fix links MyST cannot resolve on its own. Runs before MyST (priority 9).

    * link to a directory inside base     -> its README / index page
    * link to anything outside base       -> github.com blob/tree URL
    * link to a non-Markdown file in base -> plain link (file is copied as-is)
    """

    default_priority = 8

    def run(self, **kwargs) -> None:
        cfg = self.config
        srcdir = Path(self.env.srcdir)
        base_dir = srcdir / cfg.ghdocs_base if cfg.ghdocs_base else srcdir
        docs = set(self.env.found_docs)
        for node in list(self.document.findall(addnodes.pending_xref)) + list(
            self.document.findall(addnodes.download_reference)
        ):
            if node.get("reftype") != "myst":
                continue
            refdoc = node.get("refdoc", self.env.docname)
            if node.get("refdomain") == "doc":
                if node["reftarget"] in docs:
                    continue  # normal, resolvable page link
                target = "/" + str(self.env.doc2path(node["reftarget"], base=False))
                frag = node.get("reftargetid") or ""
            elif node.get("refdomain") is None:
                target, _, frag = node["reftarget"].partition("#")
                if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target):
                    continue
            else:
                continue
            _, abspath = self.env.relfn2path(str(target), refdoc)
            path = Path(abspath).resolve()
            try:
                rel = path.relative_to(srcdir.resolve()).as_posix()
            except ValueError:
                continue  # escapes the repository; leave as is
            inside = path == base_dir.resolve() or base_dir.resolve() in path.parents

            if inside and path.is_dir():
                idx = index_of(posixpath.normpath(rel).strip("."), docs)
                if idx and isinstance(node, addnodes.pending_xref):
                    node["refdomain"] = "doc"
                    node["reftarget"] = idx
                    node["reftargetid"] = frag or None
                    continue
                inside = False  # dir without Markdown: show it on GitHub
            if (
                inside
                and path.is_file()
                and isinstance(node, addnodes.download_reference)
            ):
                docdir = (srcdir / self.env.doc2path(refdoc, base=False)).parent
                uri = Path(posixpath.relpath(path, docdir.resolve())).as_posix()
                self._replace(node, uri + (f"#{frag}" if frag else ""))
                continue
            if inside:
                continue  # genuinely broken link: let MyST warn
            # GitHub redirects blob -> tree for directories, so blob is a safe default.
            kind = "tree" if path.is_dir() else "blob"
            url = f"https://github.com/{cfg.ghdocs_repo}/{kind}/{cfg.ghdocs_ref}/{rel}"
            self._replace(node, url + (f"#{frag}" if frag else ""))

    @staticmethod
    def _replace(node: nodes.Element, uri: str) -> None:
        text = node.astext()
        ref = nodes.reference("", "", refuri=uri, internal=False)
        ref.extend(node.children or [nodes.literal(text, text)])
        node.replace_self(ref)


def on_build_finished(app: Sphinx, exc: Exception | None) -> None:
    """Copy non-Markdown files under base into the output, keeping paths."""
    if exc or app.builder.format != "html" or app.builder.name != "html":
        return
    srcdir = Path(app.srcdir)
    base_dir = srcdir / app.config.ghdocs_base if app.config.ghdocs_base else srcdir
    for f in base_dir.rglob("*"):
        rel = f.relative_to(srcdir)
        if not f.is_file() or f.suffix in MD_SUFFIXES:
            continue
        if any(p.startswith(".") for p in rel.parts):
            continue
        dest = Path(app.outdir) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)


def setup(app: Sphinx) -> dict:
    app.add_config_value("ghdocs_repo", "", "env")
    app.add_config_value("ghdocs_ref", "HEAD", "env")
    app.add_config_value("ghdocs_base", "", "env")
    app.connect("source-read", on_source_read)
    app.connect("build-finished", on_build_finished)
    app.add_transform(GitHubAlerts)
    app.add_post_transform(LinkResolver)
    return {"parallel_read_safe": True, "parallel_write_safe": True}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Build a GitHub repository's plain Markdown docs with Sphinx."
    )
    p.add_argument("repo", help="GitHub repository as OWNER/REPO")
    p.add_argument(
        "base", nargs="?", default="docs", help="docs base path (default: docs)"
    )
    p.add_argument(
        "-o",
        "--outdir",
        default="_build/html",
        help="output dir (default: _build/html)",
    )
    p.add_argument(
        "-b", "--builder", default="html", help="Sphinx builder (default: html)"
    )
    p.add_argument(
        "--ref", help="branch or tag to build (default: repository default branch)"
    )
    p.add_argument(
        "--source", type=Path, help="use an existing local checkout instead of cloning"
    )
    p.add_argument(
        "-W",
        dest="warning_is_error",
        action="store_true",
        help="turn warnings into errors",
    )
    args = p.parse_args(argv)

    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        p.error("repo must be OWNER/REPO")
    base = normalize_base(args.base)

    # Make this script importable as a Sphinx extension.
    sys.modules[EXT_NAME] = sys.modules[__name__]

    with tempfile.TemporaryDirectory(prefix="ghdocs-") as tmp:
        tmp = Path(tmp)
        if args.source:
            srcdir = tmp / "src"
            shutil.copytree(args.source, srcdir, ignore=shutil.ignore_patterns(".git"))
            ref = args.ref or git("rev-parse", "--abbrev-ref", "HEAD", cwd=args.source)
        else:
            srcdir = tmp / "src"
            ref = checkout(args.repo, base, args.ref, srcdir)

        base_dir = srcdir / base if base else srcdir
        if not base_dir.is_dir():
            sys.exit(f"error: base path '{base or '.'}' not found in {args.repo}@{ref}")
        generate_missing_indexes(srcdir, base)
        root_doc = index_of(
            base,
            {
                str(f.relative_to(srcdir).with_suffix("").as_posix())
                for f in base_dir.iterdir()
                if f.suffix in MD_SUFFIXES
            },
        )
        assert root_doc  # guaranteed by generate_missing_indexes

        include = [f"{base}/**"] if base else ["**"]
        overrides = {
            "project": args.repo.split("/")[1],
            "html_title": args.repo,
            "html_theme": "shibuya",
            "html_theme_options": {"github_url": f"https://github.com/{args.repo}"},
            # shibuya: "Edit this page" / "Copy page" link back to the repository
            # (sources live on GitHub; copying them would add a ".txt" suffix
            # to the raw URL shibuya builds)
            "html_copy_source": False,
            "html_context": {
                "source_type": "github",
                "source_user": args.repo.split("/")[0],
                "source_repo": args.repo.split("/")[1],
                "source_version": ref,
                "source_docs_path": "/",
            },
            "root_doc": root_doc,
            "extensions": ["myst_parser", "sphinxcontrib.mermaid", EXT_NAME],
            "source_suffix": {s: "markdown" for s in MD_SUFFIXES},
            "include_patterns": include,
            "exclude_patterns": ["**/node_modules", "**/.*"],
            "myst_enable_extensions": ["tasklist", "strikethrough", "linkify"],
            "myst_heading_anchors": 6,
            "myst_fence_as_directive": ["mermaid"],
            "suppress_warnings": ["myst.strikethrough"],
            "ghdocs_repo": args.repo,
            "ghdocs_ref": ref,
            "ghdocs_base": base,
        }
        outdir = Path(args.outdir).resolve()
        app = Sphinx(
            srcdir=srcdir,
            confdir=None,
            outdir=outdir,
            doctreedir=tmp / "doctrees",
            buildername=args.builder,
            confoverrides=overrides,
            warningiserror=args.warning_is_error,
            freshenv=True,
        )
        app.build()
        if app.statuscode == 0:
            print(
                f"\nbuild succeeded: {outdir / (root_doc + '.html') if args.builder == 'html' else outdir}"
            )
        return app.statuscode


if __name__ == "__main__":
    sys.exit(main())
