"""scripts/site_assets.py can never start a real harness, and its images show no local paths."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = ROOT / "src" / "harnesslab" / "bundled"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "site_assets", ROOT / "scripts" / "site_assets.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


site_assets = _load_script()


def _lab(tmp_path: Path) -> Path:
    """The sweep and grow specs the script runs: the templates `harnesslab init` copies."""
    lab = tmp_path / "my-lab"
    for kind in ("sweeps", "grow"):
        (lab / kind).mkdir(parents=True)
        shutil.copy(BUNDLED / kind / "demo-fake.yaml", lab / kind / "demo-fake.yaml")
    return lab


def test_every_scripted_command_is_offline(tmp_path: Path):
    lab = _lab(tmp_path)
    for template in site_assets.COMMANDS:
        site_assets.assert_offline([a.replace("{scratch}", str(tmp_path)) for a in template], lab)
    for _stem, _title, argv in site_assets.REPORTS:
        site_assets.assert_offline(argv, lab)
    site_assets.assert_offline(["init", "my-lab"])


@pytest.mark.parametrize(
    "argv",
    [
        ["doctor"],
        ["serve"],
        ["run", "demo"],  # every variant of the suite, Claude Code's included
        ["run", "demo", "--variants", "fake-reference,claude-default"],
        ["run", "demo", "-v", "codex-default"],
        ["ablate", "run", "bundle", "--suite", "demo"],  # defaults to claude-default
        ["run", "demo", "--variants", "fake-noop", "--yes"],
        ["run", "demo", "--variants", "fake-noop", "--home", "/elsewhere"],
    ],
)
def test_commands_that_could_start_a_real_harness_are_refused(argv: list[str]):
    with pytest.raises(ValueError):
        site_assets.assert_offline(argv)


def test_specs_with_a_real_runner_or_optimizer_are_refused(tmp_path: Path):
    (tmp_path / "sweep.yaml").write_text(
        "base_variant: {runner: fake}\nfactors:\n  harness:\n    a: {runner: claude}\n"
    )
    (tmp_path / "grow.yaml").write_text(
        "base_variant: {runner: fake}\noptimizer: {kind: claude-cli}\n"
    )
    with pytest.raises(ValueError, match="runner"):
        site_assets.assert_offline(["sweep", "run", "sweep.yaml"], tmp_path)
    with pytest.raises(ValueError, match="optimizer"):
        site_assets.assert_offline(["grow", "run", "grow.yaml"], tmp_path)


def test_the_child_environment_has_no_credentials_and_only_stubbed_harnesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    secrets = [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "HARNESSLAB_HOME",
        "HTTPS_PROXY",
    ]
    for name in secrets:
        monkeypatch.setenv(name, "secret-value")
    site_assets.write_stubs(tmp_path)
    env = site_assets.safe_env(tmp_path)
    assert not set(secrets) & env.keys()
    assert "secret-value" not in env.values()
    assert env["HOME"].startswith(str(tmp_path))
    for harness in ("claude", "codex"):
        found = shutil.which(harness, path=env["PATH"])
        assert found == str(tmp_path / "bin" / harness)
        assert subprocess.run([found, "--version"], capture_output=True).returncode == 97


def test_terminal_images_use_local_fonts_and_hide_the_scratch_path(tmp_path: Path):
    scratch = str(tmp_path)
    # rich's highlighter may split a path with colour codes; the scrub must still find it
    split = scratch[:6] + "\x1b[35m" + scratch[6:] + "\x1b[0m"
    ansi = f"home {split}/my-lab/.harnesslab\n\x1b[32m✔ pass\x1b[0m\n"
    svg = site_assets.terminal_svg(
        ansi, command="harnesslab run demo", title="Run", scratch=tmp_path
    )
    assert scratch not in svg
    assert "my-lab" in svg
    assert "@font-face" not in svg and "cdnjs" not in svg
