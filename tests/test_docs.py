"""The documentation site stays consistent with the code and with itself."""

import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LINK = re.compile(r"\]\(([^)\s]+)\)")
SITE = "https://bilgin-kocak.github.io/harness-lab/"
# The landing page uses Material-only syntax (cards, buttons, icons) that GitBook would print
# literally, so it is the one page GitBook does not list; GitBook opens on the overview.
MKDOCS_ONLY = {"index.md"}
GITBOOK_README = "start/overview.md"


class _MkDocsLoader(yaml.SafeLoader):
    """mkdocs.yml names Python objects (the emoji extension's !!python/name tags); read them
    as plain strings instead of importing anything."""


_MkDocsLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
)


def _mkdocs_config() -> dict:
    return yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_MkDocsLoader)


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _summary_pages() -> list[str]:
    text = (DOCS / "SUMMARY.md").read_text(encoding="utf-8")
    return [m for m in LINK.findall(text) if m.endswith(".md")]


def _nav_pages(node) -> set[str]:
    pages: set[str] = set()
    if isinstance(node, str):
        pages.add(node)
    elif isinstance(node, list):
        for item in node:
            pages |= _nav_pages(item)
    elif isinstance(node, dict):
        for value in node.values():
            pages |= _nav_pages(value)
    return pages


def _pages() -> list[Path]:
    return [p for p in DOCS.rglob("*.md") if "superpowers" not in p.relative_to(DOCS).parts]


def _prose(text: str) -> str:
    """Page text without front matter, fenced code and inline code."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
    text = re.sub(r"^(```|~~~).*?^\1", "", text, flags=re.S | re.M)
    return re.sub(r"`[^`\n]*`", "", text)


def test_cli_reference_is_current():
    generator = _load_script("gen_cli_reference")
    committed = (DOCS / "reference" / "cli.md").read_text(encoding="utf-8")
    assert committed == generator.render(), (
        "docs/reference/cli.md is stale: run `uv run python scripts/gen_cli_reference.py`"
    )
    assert "## `harnesslab grow run`" in committed or "### `harnesslab grow run`" in committed


def test_summary_and_mkdocs_nav_list_the_same_pages():
    summary = set(_summary_pages())
    nav = _nav_pages(_mkdocs_config()["nav"])
    assert nav == summary | MKDOCS_ONLY, (
        f"only in SUMMARY: {summary - nav}; only in mkdocs nav: {nav - summary - MKDOCS_ONLY}"
    )
    assert not summary & MKDOCS_ONLY, "the landing page is MkDocs-only; GitBook must not list it"
    on_disk = {p.relative_to(DOCS).as_posix() for p in _pages() if p.name != "SUMMARY.md"}
    assert on_disk == nav, f"unlisted: {on_disk - nav}; missing files: {nav - on_disk}"


def test_gitbook_opens_on_the_overview():
    assert _summary_pages()[0] == GITBOOK_README
    for config in (ROOT / ".gitbook.yaml", DOCS / ".gitbook.yaml"):
        structure = yaml.safe_load(config.read_text(encoding="utf-8"))["structure"]
        assert structure == {"readme": GITBOOK_README, "summary": "SUMMARY.md"}, config


def test_gitbook_pages_stay_portable():
    """Pages GitBook shows use plain Markdown: Material-only syntax would appear as literal
    text there, and a link to the MkDocs-only landing page would be dead."""
    material_only = {
        "admonition": re.compile(r"^(!!!|\?\?\?)\s", re.M),
        "attribute list": re.compile(r"\]\([^)]*\)\{|^\{:?\s*[.#][\w-]", re.M),
        "markdown in HTML": re.compile(r"<\w+[^>]*\bmarkdown\b"),
        "icon shortcode": re.compile(r":(material|octicons|fontawesome|simple)-[\w-]+:"),
    }
    problems = []
    for name in _summary_pages():
        page = DOCS / name
        prose = _prose(page.read_text(encoding="utf-8"))
        problems += [f"{name}: {what}" for what, rx in material_only.items() if rx.search(prose)]
        for target in LINK.findall(prose):
            path = target.partition("#")[0]
            if path and "://" not in path and (page.parent / path).resolve() == DOCS / "index.md":
                problems.append(f"{name}: links to the landing page ({target})")
    assert not problems, "\n".join(problems)


def test_internal_links_resolve():
    broken: list[str] = []
    for page in _pages():
        for target in LINK.findall(page.read_text(encoding="utf-8")):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            path, _, _anchor = target.partition("#")
            if not path:
                continue
            resolved = (page.parent / path).resolve()
            if not resolved.exists():
                broken.append(f"{page.relative_to(ROOT)} -> {target}")
    assert not broken, "\n".join(broken)


def test_raw_html_urls_are_absolute():
    """MkDocs rewrites and checks Markdown links only: a relative URL in raw HTML would break
    on the site without any warning."""
    raw = re.compile(r"""(?:href|src)=["'](?!https?://|#|mailto:)""")
    offenders = [p.relative_to(ROOT) for p in _pages() if raw.search(_prose(p.read_text("utf-8")))]
    assert not offenders, offenders


def test_published_site_urls_still_exist():
    """URLs already published (README and so PyPI, package metadata, error messages) point at
    pages and images that must keep existing: the site's URLs are a contract."""
    sources = [ROOT / "README.md", ROOT / "pyproject.toml", *(ROOT / "src").rglob("*.py")]
    missing = []
    for source in sources:
        for url in re.findall(re.escape(SITE) + r"[^\s)\"'>`]*", source.read_text("utf-8")):
            path = url[len(SITE) :].partition("#")[0]
            if not path:
                target = DOCS / "index.md"
            elif path.endswith("/"):
                target = DOCS / (path.rstrip("/") + ".md")
            else:
                target = DOCS / path
            if not target.exists():
                missing.append(f"{source.relative_to(ROOT)}: {url}")
    assert not missing, "\n".join(missing)


def test_theme_files_exist():
    """MkDocs does not check that the favicon or extra CSS exist; a typo fails silently."""
    config = _mkdocs_config()
    theme = config["theme"]
    if "custom_dir" in theme:
        assert (ROOT / theme["custom_dir"] / "main.html").exists()
    if "favicon" in theme:
        assert (DOCS / theme["favicon"]).exists()
    for stylesheet in config.get("extra_css", []):
        assert (DOCS / stylesheet).exists(), stylesheet


def test_site_images_are_used_and_small():
    """Every file in docs/assets is referenced somewhere (the sdist ships them all) and the
    images stay within the budget scripts/site_assets.py enforces."""
    site_assets = _load_script("site_assets")
    texts = [p.read_text("utf-8") for p in _pages()]
    texts += [(ROOT / "mkdocs.yml").read_text("utf-8"), (ROOT / "README.md").read_text("utf-8")]
    texts += [p.read_text("utf-8") for p in (ROOT / "overrides").glob("*.html")]
    corpus = "\n".join(texts)
    unused = [
        p.relative_to(ROOT)
        for p in site_assets.ASSETS.rglob("*")
        if p.is_file() and p.name not in corpus
    ]
    assert not unused, f"not referenced anywhere: {unused}"
    assert not site_assets.budget_problems()
