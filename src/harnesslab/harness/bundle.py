"""Harness bundles: the growable outer layer of a coding-agent harness.

A bundle is a plain directory (design spec, section 1)::

    harnesses/baseline/
      harness.yaml         optional manifest: name, description (service-owned)
      system_prompt.md     appended to the agent's system prompt
      skills/<name>/SKILL.md
      hooks.json           Claude Code hooks ({"hooks": {...}})
      agents/<name>.md
      fake.yaml            simulation only, read by the fake runner

Every file except ``harness.yaml`` is *content* an optimizer may edit.  The bundle hash
covers every file (path, size and bytes) so two bundles with identical content hash the
same on every machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ALLOWED_TOP_FILES = frozenset({"harness.yaml", "system_prompt.md", "hooks.json", "fake.yaml"})
ALLOWED_DIRS = ("skills", "agents")
CONTENT_EXCLUDED = frozenset({"harness.yaml"})
MAX_FILE_BYTES = 64_000
MAX_BUNDLE_BYTES = 512_000
MAX_FILES = 64
IGNORED_NAMES = frozenset({"__pycache__", ".DS_Store"})
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)


class BundleError(ValueError):
    pass


def is_allowed_path(rel: str) -> bool:
    if not rel or rel.startswith("/") or "\\" in rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    if len(parts) == 1:
        return rel in ALLOWED_TOP_FILES
    return parts[0] in ALLOWED_DIRS


def _parse_frontmatter(text: str) -> dict[str, Any] | None:
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return meta if isinstance(meta, dict) else None


def _is_skill_file(rel: str) -> bool:
    return rel.startswith("skills/") and rel.endswith("/SKILL.md") and rel.count("/") == 2


def validate_files(files: dict[str, bytes]) -> list[str]:
    """Return every rule violation of ``files`` (empty list means valid)."""
    errors: list[str] = []
    if len(files) > MAX_FILES:
        errors.append(f"too many files: {len(files)} > {MAX_FILES}")
    total = 0
    for rel in sorted(files):
        data = files[rel]
        if not is_allowed_path(rel):
            errors.append(f"path not allowed: {rel}")
            continue
        total += len(data)
        if len(data) > MAX_FILE_BYTES:
            errors.append(f"{rel}: {len(data)} bytes > {MAX_FILE_BYTES}")
            continue
        if rel == "hooks.json":
            try:
                obj = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                errors.append(f"hooks.json: invalid JSON ({exc})")
                continue
            if not isinstance(obj, dict) or not isinstance(obj.get("hooks"), dict):
                errors.append('hooks.json: expected {"hooks": {...}}')
        elif rel in ("fake.yaml", "harness.yaml"):
            try:
                obj = yaml.safe_load(data.decode("utf-8"))
            except (UnicodeDecodeError, yaml.YAMLError) as exc:
                errors.append(f"{rel}: invalid YAML ({exc})")
                continue
            if obj is not None and not isinstance(obj, dict):
                errors.append(f"{rel}: expected a mapping")
        elif _is_skill_file(rel):
            meta = _parse_frontmatter(data.decode("utf-8", errors="replace"))
            if not meta or not meta.get("name"):
                errors.append(f"{rel}: missing frontmatter with 'name'")
    if total > MAX_BUNDLE_BYTES:
        errors.append(f"bundle too large: {total} bytes > {MAX_BUNDLE_BYTES}")
    return errors


def bundle_hash(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for rel in sorted(files):
        data = files[rel]
        h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data + b"\0")
    return h.hexdigest()


@dataclass
class HarnessBundle:
    path: Path
    files: dict[str, bytes]

    @classmethod
    def load(cls, path: Path | str) -> HarnessBundle:
        root = Path(path)
        if not root.is_dir():
            raise BundleError(f"harness bundle is not a directory: {root}")
        files: dict[str, bytes] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in IGNORED_NAMES and not d.startswith(".")
            )
            for name in sorted(filenames):
                if name in IGNORED_NAMES or name.startswith("."):
                    continue
                full = Path(dirpath) / name
                rel = full.relative_to(root).as_posix()
                if full.is_symlink():
                    raise BundleError(f"symlinks are not allowed in a bundle: {rel}")
                files[rel] = full.read_bytes()
        errors = validate_files(files)
        if errors:
            raise BundleError(f"invalid harness bundle {root}: " + "; ".join(errors))
        return cls(path=root, files=files)

    @classmethod
    def from_files(cls, path: Path | str, files: dict[str, str | bytes]) -> HarnessBundle:
        raw = {k: (v.encode("utf-8") if isinstance(v, str) else v) for k, v in files.items()}
        errors = validate_files(raw)
        if errors:
            raise BundleError("invalid harness bundle: " + "; ".join(errors))
        return cls(path=Path(path), files=raw)

    @property
    def hash(self) -> str:
        return bundle_hash(self.files)

    def text(self, rel: str) -> str:
        return self.files[rel].decode("utf-8", errors="replace") if rel in self.files else ""

    @property
    def manifest(self) -> dict[str, Any]:
        data = yaml.safe_load(self.text("harness.yaml")) if "harness.yaml" in self.files else None
        return data if isinstance(data, dict) else {}

    @property
    def name(self) -> str | None:
        value = self.manifest.get("name")
        return str(value) if value else None

    @property
    def description(self) -> str | None:
        value = self.manifest.get("description")
        return str(value) if value else None

    @property
    def content_files(self) -> dict[str, str]:
        return {rel: self.text(rel) for rel in sorted(self.files) if rel not in CONTENT_EXCLUDED}

    @property
    def system_prompt(self) -> str:
        return self.text("system_prompt.md").strip()

    @property
    def skills(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rel in sorted(self.files):
            if _is_skill_file(rel):
                text = self.text(rel)
                meta = _parse_frontmatter(text) or {}
                body = _FRONTMATTER.sub("", text, count=1).strip()
                out[str(meta.get("name") or rel.split("/")[1])] = body
        return out

    @property
    def fake_config(self) -> dict[str, Any]:
        data = yaml.safe_load(self.text("fake.yaml")) if "fake.yaml" in self.files else None
        return data if isinstance(data, dict) else {}

    @property
    def has_plugin_components(self) -> bool:
        return any(
            rel == "hooks.json" or rel.startswith(("skills/", "agents/")) for rel in self.files
        )

    def file_summary(self) -> list[dict[str, Any]]:
        return [{"path": rel, "bytes": len(self.files[rel])} for rel in sorted(self.files)]

    def write_to(self, dest: Path) -> None:
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        for rel, data in self.files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def prompt_prefix(bundle: HarnessBundle) -> str:
    """System prompt plus every skill body, for harnesses that only take prompt text."""
    parts = [bundle.system_prompt] if bundle.system_prompt else []
    for name, body in bundle.skills.items():
        parts.append(f"## Skill: {name}\n\n{body}")
    return "\n\n".join(parts)


def materialize_claude_plugin(bundle: HarnessBundle, dest: Path) -> Path | None:
    """Write skills, hooks and agents as a Claude Code plugin directory (``--plugin-dir``)."""
    if not bundle.has_plugin_components:
        return None
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    (dest / ".claude-plugin").mkdir(parents=True)
    manifest = {
        "name": f"harnesslab-{bundle.hash[:12]}",
        "description": bundle.description or "Harness Lab bundle",
        "version": "0.0.0",
    }
    (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2))
    for rel, data in bundle.files.items():
        if rel.startswith(("skills/", "agents/")):
            target = dest / rel
        elif rel == "hooks.json":
            target = dest / "hooks" / "hooks.json"
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dest
