import json
from pathlib import Path

import pytest

from harnesslab.harness.bundle import (
    MAX_FILE_BYTES,
    BundleError,
    HarnessBundle,
    bundle_hash,
    is_allowed_path,
    materialize_claude_plugin,
    prompt_prefix,
    validate_files,
)


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


def test_allowed_paths():
    assert is_allowed_path("system_prompt.md") and is_allowed_path("skills/x/SKILL.md")
    assert is_allowed_path("agents/reviewer.md") and is_allowed_path("hooks.json")
    assert not is_allowed_path("notes.txt")
    assert not is_allowed_path("../x") and not is_allowed_path("skills/../../x")


def test_load_hash_and_accessors(tmp_path: Path):
    root = _write(
        tmp_path / "b",
        {
            "harness.yaml": "name: demo\ndescription: d\n",
            "system_prompt.md": "Run the tests.\n",
            "skills/tdd/SKILL.md": "---\nname: tdd\n---\nWrite a failing test first.\n",
            "hooks.json": json.dumps({"hooks": {}}),
            "fake.yaml": "solve_tasks: [a]\n",
        },
    )
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "skills" / "__pycache__").mkdir()
    (root / "skills" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    b = HarnessBundle.load(root)
    assert set(b.files) == {
        "harness.yaml",
        "system_prompt.md",
        "skills/tdd/SKILL.md",
        "hooks.json",
        "fake.yaml",
    }
    assert b.name == "demo" and b.description == "d" and b.system_prompt == "Run the tests."
    assert b.skills == {"tdd": "Write a failing test first."}
    assert b.fake_config == {"solve_tasks": ["a"]} and b.has_plugin_components
    assert "harness.yaml" not in b.content_files and "system_prompt.md" in b.content_files
    assert b.hash == bundle_hash(b.files) and len(b.hash) == 64
    same = HarnessBundle.from_files(tmp_path / "other", dict(b.files))
    assert same.hash == b.hash
    assert prompt_prefix(b) == "Run the tests.\n\n## Skill: tdd\n\nWrite a failing test first."
    assert b.file_summary()[0] == {"path": "fake.yaml", "bytes": 17}


def test_empty_bundle_is_valid(tmp_path: Path):
    (tmp_path / "e").mkdir()
    b = HarnessBundle.load(tmp_path / "e")
    assert b.files == {} and b.system_prompt == "" and not b.has_plugin_components
    assert b.name is None and b.fake_config == {} and prompt_prefix(b) == ""


def test_validation_errors(tmp_path: Path):
    assert validate_files({"notes.txt": b"x"}) == ["path not allowed: notes.txt"]
    assert any("invalid JSON" in e for e in validate_files({"hooks.json": b"{"}))
    assert any("expected" in e for e in validate_files({"hooks.json": b"[]"}))
    assert any("frontmatter" in e for e in validate_files({"skills/a/SKILL.md": b"no meta"}))
    big = {"system_prompt.md": b"x" * (MAX_FILE_BYTES + 1)}
    assert any("bytes" in e for e in validate_files(big))
    assert any("mapping" in e for e in validate_files({"fake.yaml": b"- a\n"}))
    root = _write(tmp_path / "bad", {"other.md": "x"})
    with pytest.raises(BundleError, match="other.md"):
        HarnessBundle.load(root)
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "system_prompt.md").symlink_to(tmp_path / "bad" / "other.md")
    with pytest.raises(BundleError, match="symlink"):
        HarnessBundle.load(tmp_path / "s")
    with pytest.raises(BundleError, match="not a directory"):
        HarnessBundle.load(tmp_path / "missing")


def test_write_to_and_plugin(tmp_path: Path):
    b = HarnessBundle.from_files(
        tmp_path / "src",
        {
            "system_prompt.md": "p",
            "skills/a/SKILL.md": "---\nname: a\n---\nbody",
            "hooks.json": json.dumps({"hooks": {"Stop": []}}),
            "agents/r.md": "reviewer",
        },
    )
    b.write_to(tmp_path / "copy")
    assert HarnessBundle.load(tmp_path / "copy").hash == b.hash
    plugin = materialize_claude_plugin(b, tmp_path / "plugin")
    assert plugin == tmp_path / "plugin"
    manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == f"harnesslab-{b.hash[:12]}" and manifest["version"] == "0.0.0"
    assert (plugin / "skills" / "a" / "SKILL.md").read_text().endswith("body")
    assert json.loads((plugin / "hooks" / "hooks.json").read_text()) == {"hooks": {"Stop": []}}
    assert (plugin / "agents" / "r.md").read_text() == "reviewer"
    assert not (plugin / "system_prompt.md").exists()
    prompt_only = HarnessBundle.from_files(tmp_path / "np", {"system_prompt.md": "x"})
    assert materialize_claude_plugin(prompt_only, tmp_path / "p2") is None
