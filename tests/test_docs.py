"""The documentation site stays consistent with the code and with itself."""

import importlib.util
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LINK = re.compile(r"\]\(([^)\s]+)\)")


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "gen_cli_reference", ROOT / "scripts" / "gen_cli_reference.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _summary_pages() -> set[str]:
    text = (DOCS / "SUMMARY.md").read_text(encoding="utf-8")
    return {m for m in LINK.findall(text) if m.endswith(".md")}


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


def test_cli_reference_is_current():
    generator = _load_generator()
    committed = (DOCS / "reference" / "cli.md").read_text(encoding="utf-8")
    assert committed == generator.render(), (
        "docs/reference/cli.md is stale: run `uv run python scripts/gen_cli_reference.py`"
    )
    assert "## `harnesslab grow run`" in committed or "### `harnesslab grow run`" in committed


def test_summary_and_mkdocs_nav_list_the_same_pages():
    summary = _summary_pages()
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    nav = _nav_pages(config["nav"])
    assert summary == nav, f"only in SUMMARY: {summary - nav}; only in mkdocs nav: {nav - summary}"
    on_disk = {
        p.relative_to(DOCS).as_posix()
        for p in DOCS.rglob("*.md")
        if p.name != "SUMMARY.md" and "superpowers" not in p.relative_to(DOCS).parts
    }
    assert on_disk == summary, f"unlisted: {on_disk - summary}; missing files: {summary - on_disk}"
    assert (ROOT / ".gitbook.yaml").exists()


def test_internal_links_resolve():
    broken: list[str] = []
    for page in DOCS.rglob("*.md"):
        if "superpowers" in page.relative_to(DOCS).parts:
            continue
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
