#!/usr/bin/env python
"""Regenerate the website's images from the bundled demos, offline and without API keys.

    uv run --with "playwright==1.56.*" --with pillow python scripts/site_assets.py
    uv run --with "playwright==1.56.*" --with pillow python scripts/site_assets.py --only terminal
    uv run --with "playwright==1.56.*" python scripts/site_assets.py verify --site site --out DIR

The first form runs the bundled demo suites against the offline fake agents in a scratch
directory, then writes to ``docs/assets/``: terminal images of the CLI output, dashboard
screenshots, the favicon and the link-preview card. ``verify`` serves a built site
(``mkdocs build``) and screenshots its main pages at desktop and phone widths in both colour
schemes, checking that nothing overflows, every image loads and the browser logs no errors.

No real harness can start: ``claude`` and ``codex`` on the child PATH are stubs that exit 97,
the child environment is built from scratch without credentials, proxy settings or
``HARNESSLAB_HOME``, and every command is checked against an allowlist of fake variants before
it runs. Playwright 1.56 matches the pre-installed Chromium (``/opt/pw-browsers``); pass
``--chromium PATH`` to use another browser binary. ``tests/test_site_assets.py`` checks the
guards and ``tests/test_docs.py`` the size budget below.
"""

from __future__ import annotations

import argparse
import base64
import functools
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.text import Text

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
SCREENSHOTS = ASSETS / "screenshots"
TERMINAL = ASSETS / "terminal"
BRAND = ASSETS / "brand"

# Images are committed and the sdist ships docs/: keep them small.
MAX_RASTER_BYTES = 300_000
MAX_SVG_BYTES = 200_000
MAX_TOTAL_BYTES = 2_000_000

PAID_NOTICE = "may use paid API usage"  # what the CLI prints before starting a paid harness
OFFLINE_OPTIMIZERS = frozenset({"fake", "manual"})
VARIANT_FLAGS = frozenset({"--variants", "-v", "--variant"})
READ_ONLY = frozenset(
    {("experiment", "compare"), ("sweep", "report"), ("grow", "show"), ("ablate", "report")}
)
STUB = (
    "#!/bin/sh\necho 'blocked by scripts/site_assets.py: no real harness runs here' >&2\nexit 97\n"
)
INDIGO = "#4051b5"  # the site's primary colour (Material's indigo)
ICON = "material/flask-outline.svg"  # the site's logo, from the icons Material ships

# The experiments behind the images, oldest first (the dashboard lists the newest on top).
# Every run names its fake variants; "{scratch}" is the scratch directory.
FIRST_RUN = ["run", "demo", "--variants", "fake-reference,fake-noop"]
COMMANDS: list[list[str]] = [
    ["grow", "run", "grow/demo-fake.yaml"],
    ["sweep", "run", "sweeps/demo-fake.yaml"],
    ["run", "suites/demo-safety/suite.yaml", "--name", "safety",
     "--variants", "fake-careful,fake-reckless,fake-reckless-sentinel"],
    ["run", "suites/demo-improve/suite.yaml", "--name", "improvement",
     "--variants", "fake-improver,fake-breaker,fake-noop"],
    ["run", "suites/demo-recovery/suite.yaml", "--name", "recovery", "-r", "2",
     "--variants", "fake-careful,fake-naive,fake-noop"],
    ["run", "suites/demo-discovery/suite.yaml", "--name", "discovery",
     "--variants", "fake-reference,fake-partial,fake-noop"],
    ["ablate", "run", "{scratch}/ablation/bundle", "--suite", "{scratch}/ablation/suite.yaml",
     "--variant", "fake-noop", "-r", "2", "--name", "ablation"],
    FIRST_RUN,
    ["run", "demo", "--name", "demo-four-agents", "-r", "3",
     "--variants", "fake-reference,fake-noop,fake-flaky,fake-partial"],
]  # fmt: skip
# Terminal images of report commands: (file stem, window title, argv).
REPORTS: list[tuple[str, str, list[str]]] = [
    ("experiment-compare", "Paired comparison",
     ["experiment", "compare", "demo-four-agents", "fake-flaky", "fake-reference"]),
    ("grow-show", "Grow session", ["grow", "show", "demo-fake-grow"]),
    ("ablate-report", "Ablation report", ["ablate", "report", "ablation"]),
]  # fmt: skip
ABLATION_TASKS = [f"t{i}" for i in range(1, 7)]

WIDTH = 1100  # CSS px of the dashboard screenshots, captured at 2x
PAD = 16
VERIFY_PAGES = ["/", "/start/overview/", "/start/tour/", "/guides/sweeps/", "/reference/cli/"]
VERIFY_SIZES = [(1440, 900), (390, 844)]


# ---------------------------------------------------------------------------
# Guards: nothing here may start a real harness
# ---------------------------------------------------------------------------


def _values(node: Any, key: str) -> Iterator[Any]:
    """Every value stored under ``key`` anywhere in a parsed YAML document."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _values(v, key)
    elif isinstance(node, list):
        for item in node:
            yield from _values(item, key)


def assert_offline(argv: Sequence[str], lab: Path | None = None) -> None:
    """Refuse a command that could start a real harness.

    Runs and ablations must name fake variants explicitly; sweep and grow specs may only use
    the fake runner and a fake or manual optimizer; reports are read-only. Anything else,
    ``doctor`` included (it probes the installed CLIs), is refused, and so are ``--home`` and
    ``--yes``.
    """
    args = list(argv)
    shown = " ".join(args)
    if {"--home", "--yes", "-y"} & set(args):
        raise ValueError(f"{shown}: --home and --yes are not allowed here")
    if args[:1] == ["init"]:
        return
    if args[:1] == ["run"] or args[:2] == ["ablate", "run"]:
        named = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg in VARIANT_FLAGS]
        variants = [v.strip() for value in named for v in value.split(",") if v.strip()]
        if not variants:
            raise ValueError(f"{shown}: name the fake variants explicitly")
        if bad := [v for v in variants if not v.startswith("fake-")]:
            raise ValueError(f"{shown}: not a fake variant: {', '.join(bad)}")
        return
    if args[:2] in (["sweep", "run"], ["grow", "run"]) and len(args) >= 3:
        if lab is None:
            raise ValueError(f"{shown}: the spec can only be checked inside the lab")
        spec = yaml.safe_load((lab / args[2]).read_text(encoding="utf-8")) or {}
        runners: set[str] = set()
        for value in _values(spec, "runner"):
            runners.update(str(v) for v in (value if isinstance(value, list) else [value]))
        if runners != {"fake"}:
            raise ValueError(
                f"{args[2]}: runner {sorted(runners)}; only the fake runner is allowed"
            )
        kind = (spec.get("optimizer") or {}).get("kind")
        if args[0] == "grow" and kind not in OFFLINE_OPTIMIZERS:
            raise ValueError(f"{args[2]}: optimizer {kind!r}; only fake or manual is allowed")
        return
    if tuple(args[:2]) in READ_ONLY:
        return
    raise ValueError(f"{shown}: not on the allowlist")


def write_stubs(scratch: Path) -> Path:
    """``claude`` and ``codex`` stubs that refuse to run, first on the child PATH."""
    bin_dir = scratch / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "codex"):
        stub = bin_dir / name
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
    return bin_dir


def safe_env(scratch: Path) -> dict[str, str]:
    """The child environment, built from nothing rather than filtered.

    The stubs and this Python come first on PATH; HOME is a scratch directory (no
    ``~/.claude`` or ``~/.codex``); no credentials, proxy settings or ``HARNESSLAB_HOME``.
    UTC, UTF-8 and a 100-column terminal make rich print the same tables everywhere.
    """
    home = scratch / "home"
    home.mkdir(parents=True, exist_ok=True)
    path = [str(scratch / "bin"), str(Path(sys.executable).parent)]
    path += [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return {
        "PATH": os.pathsep.join(path),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
        "PYTHONUTF8": "1",
        "TERM": "xterm-256color",
        "COLUMNS": "100",
        "TTY_COMPATIBLE": "1",  # rich keeps its colours although stdout is a pipe,
        "TTY_INTERACTIVE": "0",  # but draws no live progress
    }


def run_cli(argv: Sequence[str], *, cwd: Path, env: dict[str, str], lab: Path | None = None) -> str:
    """Run one harnesslab command after the offline check; returns its coloured stdout."""
    assert_offline(argv, lab)
    proc = subprocess.run(
        [sys.executable, "-m", "harnesslab", *argv],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if PAID_NOTICE in proc.stdout + proc.stderr:
        raise SystemExit(f"stopped: `harnesslab {' '.join(argv)}` announced a paid harness")
    if proc.returncode != 0:
        raise SystemExit(
            f"`harnesslab {' '.join(argv)}` failed with exit {proc.returncode}:\n"
            + proc.stderr[-3000:]
        )
    return proc.stdout


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------


def write_ablation_suite(root: Path) -> None:
    """Six synthetic tasks and a two-component bundle (as in tests/test_ablation.py): the
    skill solves every task, the system prompt only costs LLM calls. Six tasks, because a
    verdict needs at least five."""
    fixture = root / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "README.md").write_text("# tiny\n", encoding="utf-8")
    for task in ABLATION_TASKS:
        overlay = root / "tasks" / task / "solution"
        overlay.mkdir(parents=True)
        (overlay / f"solved_{task}").write_text("ok\n", encoding="utf-8")
        (root / "tasks" / f"{task}.yaml").write_text(
            f"id: {task}\nname: Synthetic task {task[1:]}\nrepo:\n  path: ../fixture\n"
            "prompt: Create the file.\n"
            f"verification:\n  command: test -f solved_{task}\n  timeout_seconds: 30\n"
            f"reference_solution:\n  overlay: {task}/solution\n",
            encoding="utf-8",
        )
    (root / "suite.yaml").write_text(
        "name: synthetic-six\ntasks:\n"
        + "".join(f"  - tasks/{t}.yaml\n" for t in ABLATION_TASKS)
        + "variants:\n  - id: fake-noop\n    runner: fake\n    behavior: noop\n",
        encoding="utf-8",
    )
    bundle = root / "bundle"
    (bundle / "skills" / "careful").mkdir(parents=True)
    (bundle / "skills" / "careful" / "SKILL.md").write_text(
        "---\nname: careful\n---\nRead first.\n", encoding="utf-8"
    )
    (bundle / "system_prompt.md").write_text("Think about the task.\n", encoding="utf-8")
    (bundle / "fake.yaml").write_text(
        "component_solves:\n  skills/careful: [" + ", ".join(ABLATION_TASKS) + "]\n"
        "component_llm_calls:\n  system_prompt.md: 3\n",
        encoding="utf-8",
    )


def build_lab(scratch: Path, env: dict[str, str]) -> tuple[Path, str]:
    """Run every demo experiment in a fresh lab; returns the lab and the first run's output."""
    run_cli(["init", "my-lab"], cwd=scratch, env=env)
    lab = scratch / "my-lab"
    write_ablation_suite(scratch / "ablation")
    first_output = ""
    for template in COMMANDS:
        argv = [arg.replace("{scratch}", str(scratch)) for arg in template]
        print("  harnesslab " + " ".join(argv).replace(str(scratch), "~"), flush=True)
        output = run_cli(argv, cwd=lab, env=env, lab=lab)
        if template is FIRST_RUN:
            first_output = output
    return lab, first_output


# ---------------------------------------------------------------------------
# Terminal images
# ---------------------------------------------------------------------------

SVG_FONTS = (
    'ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", "Liberation Mono", '
    "monospace"
)
_ANSI = r"(?:\x1b\[[0-9;]*m)*"


def scrub_path(ansi: str, prefix: str, replacement: str = "~") -> str:
    """Replace ``prefix`` in coloured text, also where rich's highlighting split it."""
    pattern = _ANSI.join(re.escape(char) for char in prefix)
    return re.sub(pattern, replacement, ansi)


@functools.cache
def _svg_format() -> str:
    """rich's SVG template without its web fonts: readers use their own monospace font."""
    from rich._export_format import CONSOLE_SVG_FORMAT

    without_fonts = re.sub(r"[ \t]*@font-face \{\{.*?\}\}\n", "", CONSOLE_SVG_FORMAT, flags=re.S)
    return without_fonts.replace("font-family: Fira Code, monospace;", f"font-family: {SVG_FONTS};")


def terminal_svg(
    ansi: str, *, command: str, title: str, scratch: Path | None = None, width: int = 100
) -> str:
    """Terminal output as an SVG window: the command, then its output in colour, with the
    scratch directory shown as ``~``."""
    if scratch is not None:
        ansi = scrub_path(ansi, str(scratch))
    console = Console(
        record=True,
        width=width,
        force_terminal=True,
        color_system="truecolor",
        file=io.StringIO(),
    )
    console.print(Text("$ " + command, style="bold"))
    console.print(Text.from_ansi(ansi.rstrip()), soft_wrap=True)
    unique = "hl-" + re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return console.export_svg(title=title, code_format=_svg_format(), unique_id=unique)


def assert_no_leaks(text: str, scratch: Path) -> None:
    """Refuse an image that shows the scratch directory, the real home or the host name."""
    for secret in (str(scratch), str(Path.home()), socket.gethostname()):
        if len(secret) > 3 and secret in text:
            raise SystemExit(f"refusing to write an image that shows {secret!r}")


def write_terminal_images(lab: Path, env: dict[str, str], first_output: str, scratch: Path) -> None:
    TERMINAL.mkdir(parents=True, exist_ok=True)
    images = [("run-demo", "First run", "harnesslab " + " ".join(FIRST_RUN), first_output)]
    for stem, title, argv in REPORTS:
        output = run_cli(argv, cwd=lab, env=env, lab=lab)
        images.append((stem, title, "harnesslab " + " ".join(argv), output))
    for stem, title, command, output in images:
        svg = terminal_svg(output, command=command, title=title, scratch=scratch)
        assert_no_leaks(svg, scratch)
        (TERMINAL / f"{stem}.svg").write_text(svg, encoding="utf-8")


# ---------------------------------------------------------------------------
# Dashboard screenshots
# ---------------------------------------------------------------------------

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost: no proxy


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def dashboard(lab: Path, env: dict[str, str]) -> Iterator[str]:
    """``harnesslab serve`` for the lab on a free port; yields its base URL."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "harnesslab", "serve", "--port", str(port)],
        cwd=lab,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise SystemExit("harnesslab serve exited before it answered")
            try:
                with OPENER.open(base + "/healthz", timeout=2):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise SystemExit("harnesslab serve did not answer within 30 s") from None
                time.sleep(0.2)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_url(base: str, experiment: str, variant: str, task: str | None = None) -> str:
    """The dashboard URL of the first repetition of ``variant`` (on ``task``)."""
    with OPENER.open(f"{base}/api/experiments/{experiment}/runs.json", timeout=10) as resp:
        runs = json.load(resp)["runs"]
    for sample in runs:
        if sample["variant_key"] == variant and sample["repetition"] == 0:
            if task is None or sample["task_key"] == task:
                return f"/runs/{sample['run_id']}"
    raise SystemExit(f"no run of {variant} in {experiment}")


def _launch(pw: Any, chromium: str | None) -> Any:
    path = chromium or os.environ.get("HARNESSLAB_CHROMIUM")
    return pw.chromium.launch(executable_path=path) if path else pw.chromium.launch()


def _box(page: Any, selector: str) -> dict[str, float]:
    box = page.locator(selector).first.bounding_box()
    if box is None:
        raise SystemExit(f"{selector!r} is not on {page.url}")
    return box


def capture_dashboard(base: str, chromium: str | None) -> dict[str, bytes]:
    """PNG screenshots of every dashboard view, cropped from a section (or the page top) to
    the end of another and never past the reproducibility record, which shows local paths."""
    from playwright.sync_api import sync_playwright

    four = "demo-four-agents"
    run = run_url(base, four, "fake-reference", task="add-tag-budgets")

    def section(heading: str) -> str:
        return f'section:has(h2:has-text("{heading}"))'

    plan: list[tuple[str, str, str | None, str, int]] = [
        # name, URL, crop start (None: the page top), crop end, maximum height in CSS px
        ("experiments", "/", None, "table.data tbody tr:nth-child(6)", 900),
        ("experiment-matrix", f"/experiments/{four}", None, "table.matrix", 1500),
        ("attempts", f"/experiments/{four}", "section.attempts", "section.attempts", 900),
        ("compare", f"/experiments/{four}/compare?a=fake-flaky&b=fake-reference", None,
         section("Statistical evidence"), 1500),
        ("run", run, None, section("Timeline"), 1500),
        ("run-diff", run, section("Git diff"), f"{section('Verifier output')} table.kv", 1300),
        ("sweep", "/experiments/demo-fake-sweep", "section.sweep", "section.sweep .cards", 900),
        ("sweep-configs", "/experiments/demo-fake-sweep", "section.sweep details",
         "section.sweep details", 1000),
        ("grow-list", "/grow", None, "table.data", 700),
        ("grow", "/grow/demo-fake-grow", None, section("Harness versions"), 1500),
        ("ablation", "/experiments/ablation", "section.ablation", "section.ablation", 1200),
        ("safety", run_url(base, "safety", "fake-reckless"), section("Safety"),
         section("Safety"), 1200),
        ("improvement", run_url(base, "improvement", "fake-improver"), section("Improvement"),
         section("Improvement"), 1200),
        ("recovery", "/experiments/recovery", "section.recovery", "section.recovery", 900),
    ]  # fmt: skip
    shots: dict[str, bytes] = {}
    with sync_playwright() as pw:
        browser = _launch(pw, chromium)
        context = browser.new_context(
            viewport={"width": WIDTH, "height": 800},
            device_scale_factor=2,
            color_scheme="light",
            locale="en-US",
            timezone_id="UTC",
            reduced_motion="reduce",
        )
        context.route(
            "**/*",
            lambda route: (
                route.continue_() if route.request.url.startswith(base) else route.abort()
            ),
        )
        page = context.new_page()
        for name, url, top, bottom, max_height in plan:
            page.goto(base + url)
            page.wait_for_load_state("networkidle")
            page.evaluate("document.fonts.ready.then(() => true)")
            y0 = 0.0 if top is None else max(0.0, _box(page, top)["y"] - PAD)
            end = _box(page, bottom)
            height = min(end["y"] + end["height"] + PAD - y0, max_height)
            clip = {"x": 0, "y": y0, "width": WIDTH, "height": height}
            shots[name] = page.screenshot(clip=clip, full_page=True)
        browser.close()
    return shots


def encode_webp(png: bytes) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(out, "WEBP", quality=80, method=6)
    return out.getvalue()


def encode_png(png: bytes, max_bytes: int = MAX_RASTER_BYTES) -> bytes:
    """A PNG small enough for the budget, for readers outside the site (README, PyPI, the
    link-preview card): full colour if it fits, else 256 colours, then smaller."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    for scale in (1.0, 0.85, 0.7, 0.55):
        sized = image
        if scale < 1.0:
            size = (round(image.width * scale), round(image.height * scale))
            sized = image.resize(size, Image.Resampling.LANCZOS)
        flat = sized.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        for candidate in (sized, flat):
            out = io.BytesIO()
            candidate.save(out, "PNG", optimize=True)
            if out.tell() <= max_bytes:
                return out.getvalue()
    raise SystemExit("the hero screenshot does not fit the size budget")


def write_screenshots(shots: dict[str, bytes]) -> None:
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    for name, png in shots.items():
        if name == "experiment-matrix":  # the hero: also used by README, PyPI and the card
            (SCREENSHOTS / f"{name}.png").write_bytes(encode_png(png))
        else:
            (SCREENSHOTS / f"{name}.webp").write_bytes(encode_webp(png))


# ---------------------------------------------------------------------------
# Favicon and link-preview card
# ---------------------------------------------------------------------------


def icon_path_data() -> str:
    """The logo's SVG path, from the Material Design icons the docs theme ships (Apache-2.0)."""
    import material

    svg = (Path(material.__file__).parent / "templates" / ".icons" / ICON).read_text("utf-8")
    match = re.search(r' d="([^"]+)"', svg)
    if match is None:
        raise SystemExit(f"no path in {ICON}")
    return match.group(1)


def favicon_html(path_data: str) -> str:
    return f"""<!doctype html><html><body style="margin:0;background:transparent">
<div id="shot" style="width:64px;height:64px;border-radius:14px;background:{INDIGO};
  display:flex;align-items:center;justify-content:center">
<svg viewBox="0 0 24 24" width="44" height="44"><path fill="#fff" d="{path_data}"/></svg>
</div></body></html>"""


def social_card_html(path_data: str, hero_png: bytes) -> str:
    hero = base64.b64encode(hero_png).decode("ascii")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin: 0; }}
#shot {{ position: relative; width: 1200px; height: 630px; overflow: hidden; color: #fff;
  background: linear-gradient(135deg, #283593 0%, {INDIGO} 55%, #5c6bc0 100%);
  font-family: "Helvetica Neue", Arial, "Liberation Sans", "DejaVu Sans", sans-serif; }}
.text {{ position: absolute; left: 72px; top: 70px; width: 500px; }}
.brand {{ display: flex; align-items: center; gap: 18px; font-size: 64px; font-weight: 700;
  letter-spacing: -1px; }}
.brand svg {{ width: 64px; height: 64px; }}
.tagline {{ margin-top: 30px; font-size: 34px; line-height: 1.25; font-weight: 600; }}
.points {{ margin-top: 26px; font-size: 22px; line-height: 1.5; opacity: .9; }}
.install {{ display: inline-block; margin-top: 34px; padding: 12px 20px; border-radius: 10px;
  background: rgba(0,0,0,.28); font: 24px "DejaVu Sans Mono", Menlo, monospace; }}
.shot {{ position: absolute; left: 620px; top: 70px; width: 720px; border-radius: 12px;
  box-shadow: 0 18px 48px rgba(0,0,0,.35); }}
</style></head><body><div id="shot">
<div class="text">
  <div class="brand"><svg viewBox="0 0 24 24"><path fill="#fff" d="{path_data}"/></svg>
    Harness Lab</div>
  <div class="tagline">Controlled experiments for AI coding-agent harnesses</div>
  <div class="points">Same task, same commit, hidden tests.<br>
    Cost, LLM calls and paired statistics.</div>
  <div class="install">pip install harnesslab</div>
</div>
<img class="shot" src="data:image/png;base64,{hero}" alt="">
</div></body></html>"""


def write_brand(chromium: str | None) -> None:
    from playwright.sync_api import sync_playwright

    BRAND.mkdir(parents=True, exist_ok=True)
    path_data = icon_path_data()
    hero = (SCREENSHOTS / "experiment-matrix.png").read_bytes()
    with sync_playwright() as pw:
        browser = _launch(pw, chromium)
        page = browser.new_page(device_scale_factor=1)
        page.set_content(favicon_html(path_data))
        (BRAND / "favicon.png").write_bytes(page.locator("#shot").screenshot(omit_background=True))
        page.set_viewport_size({"width": 1200, "height": 630})
        page.set_content(social_card_html(path_data, hero))
        page.wait_for_load_state("load")
        # JPEG: a gradient and a screenshot compress far better than as PNG, and every
        # link-preview crawler reads it
        (BRAND / "social-card.jpg").write_bytes(
            page.locator("#shot").screenshot(type="jpeg", quality=90)
        )
        browser.close()


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def budget_problems(root: Path = ASSETS) -> list[str]:
    """Files over their size limit, and the total over its limit."""
    files = sorted(p for p in root.rglob("*") if p.is_file())
    problems = []
    for path in files:
        limit = MAX_SVG_BYTES if path.suffix == ".svg" else MAX_RASTER_BYTES
        if path.stat().st_size > limit:
            problems.append(f"{path.relative_to(ROOT)}: {path.stat().st_size} bytes > {limit}")
    total = sum(p.stat().st_size for p in files)
    if total > MAX_TOTAL_BYTES:
        problems.append(f"{root.relative_to(ROOT)}: {total} bytes in total > {MAX_TOTAL_BYTES}")
    return problems


def report() -> int:
    total = 0
    for path in sorted(p for p in ASSETS.rglob("*") if p.is_file()):
        size = path.stat().st_size
        total += size
        print(f"  {path.relative_to(ROOT)}  {size / 1000:.0f} kB")
    print(f"  total {total / 1000:.0f} kB")
    problems = budget_problems()
    for problem in problems:
        print(f"over budget: {problem}", file=sys.stderr)
    return 1 if problems else 0


# ---------------------------------------------------------------------------
# Verify a built site
# ---------------------------------------------------------------------------


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


def verify(site: Path, out: Path, chromium: str | None) -> int:
    """Screenshot the built site's main pages (desktop and phone, light and dark) into
    ``out`` and check them; returns 1 when a check fails."""
    from playwright.sync_api import sync_playwright

    if not (site / "index.html").exists():
        raise SystemExit(f"{site} is not a built site; run `mkdocs build` first")
    out.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(site))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def offline(route: Any) -> None:
        # Pages are checked as served by this site alone: Google Fonts and the GitHub API
        # (repository stars) get empty answers, so the fallback fonts show.
        url = route.request.url
        if url.startswith(base):
            route.continue_()
        elif "api.github.com" in url:
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.fulfill(status=200, body="")

    problems: list[str] = []
    with sync_playwright() as pw:
        browser = _launch(pw, chromium)
        for scheme in ("light", "dark"):
            for width, height in VERIFY_SIZES:
                context = browser.new_context(
                    viewport={"width": width, "height": height}, color_scheme=scheme
                )
                context.route("**/*", offline)
                for path in VERIFY_PAGES:
                    where = f"{path} at {width}px, {scheme}"
                    page = context.new_page()
                    errors: list[str] = []
                    page.on("console", lambda m, e=errors: e.append(m.text) if m.type == "error" else None)  # fmt: skip
                    page.on("pageerror", lambda exc, e=errors: e.append(str(exc)))
                    page.goto(base + path)
                    page.wait_for_load_state("networkidle")
                    problems += [f"{where}: console: {e}" for e in errors]
                    if not page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    ):
                        problems.append(f"{where}: the page scrolls sideways")
                    if not page.evaluate(
                        "[...document.images].every(i => i.complete && i.naturalWidth > 0)"
                    ):
                        problems.append(f"{where}: an image did not load")
                    if path == "/":
                        problems += _check_home(page, where, width)
                    slug = path.strip("/").replace("/", "-") or "home"
                    page.screenshot(
                        path=str(out / f"{slug}-{width}-{scheme}.png"), full_page=path == "/"
                    )
                    page.close()
                context.close()
        browser.close()
    server.shutdown()
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"{len(VERIFY_PAGES) * len(VERIFY_SIZES) * 2} page views checked, screenshots in {out}")
    return 1 if problems else 0


def _check_home(page: Any, where: str, width: int) -> list[str]:
    problems = []
    if (count := page.locator("h1").count()) != 1:
        problems.append(f"{where}: {count} h1 elements, not 1")
    if page.locator(".md-content__button").first.is_visible():
        problems.append(f"{where}: the edit button shows on the landing page")
    image = page.locator('meta[property="og:image"]').get_attribute("content") or ""
    if not image.startswith("https://"):
        problems.append(f"{where}: og:image is {image!r}, not an absolute URL")
    if width >= 1220 and not page.locator(".md-tabs").is_visible():
        problems.append(f"{where}: the navigation tabs are hidden")
    return problems


# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the website's images offline, or check a built site."
    )
    parser.add_argument("mode", nargs="?", choices=["generate", "verify"], default="generate")
    parser.add_argument("--only", choices=["terminal", "screenshots", "brand"])
    parser.add_argument("--keep", action="store_true", help="keep the scratch lab")
    parser.add_argument("--chromium", help="browser binary (default: Playwright's own)")
    parser.add_argument("--site", type=Path, default=ROOT / "site", help="verify: built site")
    parser.add_argument("--out", type=Path, help="verify: where screenshots go")
    args = parser.parse_args(argv)

    if args.mode == "verify":
        out = args.out or Path(tempfile.mkdtemp(prefix="hl-verify-"))
        return verify(args.site, out, args.chromium)

    scratch = Path(tempfile.mkdtemp(prefix="hl-"))
    try:
        write_stubs(scratch)
        env = safe_env(scratch)
        if args.only in (None, "terminal", "screenshots"):
            print("Running the demo experiments with the offline fake agents:")
            lab, first_output = build_lab(scratch, env)
            if args.only in (None, "terminal"):
                print("Writing terminal images")
                write_terminal_images(lab, env, first_output, scratch)
            if args.only in (None, "screenshots"):
                print("Taking dashboard screenshots")
                with dashboard(lab, env) as base:
                    write_screenshots(capture_dashboard(base, args.chromium))
        if args.only in (None, "brand"):
            print("Drawing the favicon and the link-preview card")
            write_brand(args.chromium)
    finally:
        if args.keep:
            print(f"scratch lab kept in {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
