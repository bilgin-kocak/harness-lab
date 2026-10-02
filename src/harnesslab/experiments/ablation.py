"""Component ablation: does each part of a harness bundle earn its place?

Recent results (e.g. arXiv 2609.40303) show elaborate harnesses often add nothing over a minimal
agent with the same model, while task-adapted components sometimes help a lot.  So every
component has to beat its own absence, with paired evidence.  ``plan_ablation`` builds, from one
bundle:

* ``full``: the bundle as is;
* ``minimal``: no components (only ``harness.yaml`` and the fake runner's ``fake.yaml``, which are
  metadata and simulation, not harness);
* ``without:<component>``: the bundle minus one component, for every component
  (``system_prompt.md``, each ``skills/<name>``, each ``agents/<name>.md``, ``hooks.json``).

The variants run as one ordinary experiment.  ``report_for_experiment`` then compares, task by
task, ``full`` against each ``without:`` variant (the component's effect) and against
``minimal`` (the whole bundle's effect), using :mod:`harnesslab.experiments.stats`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harnesslab.core.models import VariantSpec
from harnesslab.experiments.aggregate import samples_from_rows
from harnesslab.experiments.stats import (
    DEFAULT_MIN_TASKS,
    DEFAULT_RESAMPLES,
    PairedComparison,
    paired_comparison,
)
from harnesslab.harness.bundle import HarnessBundle

NON_COMPONENT_FILES = frozenset({"harness.yaml", "fake.yaml"})
FULL = "full"
MINIMAL = "minimal"
WITHOUT_PREFIX = "without:"

ComponentVerdict = Literal["helps", "hurts", "no evidence", "not enough tasks"]


def bundle_components(bundle: HarnessBundle) -> list[str]:
    """Ablatable components, in a stable order."""
    files = set(bundle.files)
    components: list[str] = []
    if "system_prompt.md" in files:
        components.append("system_prompt.md")
    components.extend(
        sorted({f"skills/{p.split('/')[1]}" for p in files if p.startswith("skills/")})
    )
    components.extend(sorted(p for p in files if p.startswith("agents/")))
    if "hooks.json" in files:
        components.append("hooks.json")
    return components


def _in_component(path: str, component: str) -> bool:
    return path == component or path.startswith(component + "/")


def files_without(bundle: HarnessBundle, component: str) -> dict[str, bytes]:
    return {p: data for p, data in bundle.files.items() if not _in_component(p, component)}


def minimal_files(bundle: HarnessBundle) -> dict[str, bytes]:
    return {p: data for p, data in bundle.files.items() if p in NON_COMPONENT_FILES}


class AblationSpec(BaseModel):
    """Stored on the experiment (``spec_json["ablation"]``) so the report can be recomputed."""

    bundle: str
    bundle_hash: str
    base_variant: str
    components: list[str]
    variant_keys: dict[str, str]  # component -> variant key
    resamples: int = DEFAULT_RESAMPLES
    seed: int = 0
    min_tasks: int = DEFAULT_MIN_TASKS


def plan_ablation(
    bundle_dir: Path,
    base: VariantSpec,
    out_dir: Path,
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
    min_tasks: int = DEFAULT_MIN_TASKS,
) -> tuple[list[VariantSpec], AblationSpec]:
    """Write the full, minimal and leave-one-out bundles under ``out_dir`` and build variants."""
    bundle = HarnessBundle.load(bundle_dir)
    components = bundle_components(bundle)
    if not components:
        raise ValueError(
            f"bundle {bundle_dir} has no components to ablate "
            "(system_prompt.md, skills/<name>, agents/<name>.md or hooks.json)"
        )
    plans: list[tuple[str, dict[str, bytes]]] = [
        (FULL, dict(bundle.files)),
        (MINIMAL, minimal_files(bundle)),
    ]
    plans.extend((WITHOUT_PREFIX + c, files_without(bundle, c)) for c in components)

    base_options = {k: v for k, v in base.options.items()}
    variants: list[VariantSpec] = []
    for key, files in plans:
        built = HarnessBundle.from_files(out_dir / _slug(key), files)
        built.write_to(built.path)
        variant = VariantSpec(
            id=key,
            runner=base.runner,
            model=base.model,
            description=f"{base.id} with harness {key}",
            harness=str(built.path),
            **base_options,
        )
        variant.harness_dir = built.path.resolve()
        variant.harness_hash = built.hash
        variants.append(variant)
    spec = AblationSpec(
        bundle=str(Path(bundle_dir).resolve()),
        bundle_hash=bundle.hash,
        base_variant=base.id,
        components=components,
        variant_keys={c: WITHOUT_PREFIX + c for c in components},
        resamples=resamples,
        seed=seed,
        min_tasks=min_tasks,
    )
    return variants, spec


def _slug(key: str) -> str:
    return key.replace(":", "-").replace("/", "-")


class ComponentEffect(BaseModel):
    """The component's effect is ``full`` (B) minus ``without:<component>`` (A)."""

    component: str
    variant_key: str
    verdict: ComponentVerdict
    comparison: PairedComparison


class AblationReport(BaseModel):
    bundle: str
    bundle_hash: str
    base_variant: str
    full_vs_minimal: PairedComparison
    full_verdict: ComponentVerdict
    components: list[ComponentEffect] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


_VERDICTS: dict[str, ComponentVerdict] = {
    "better": "helps",
    "worse": "hurts",
    "no evidence": "no evidence",
    "not enough tasks": "not enough tasks",
}


def analyze_ablation(
    spec: AblationSpec, samples: list[Any], task_order: list[str]
) -> AblationReport:
    def compare(a: str) -> PairedComparison:
        return paired_comparison(
            samples,
            a,
            FULL,
            task_order=task_order,
            resamples=spec.resamples,
            seed=spec.seed,
            min_tasks=spec.min_tasks,
        )

    full_vs_minimal = compare(MINIMAL)
    effects = [
        ComponentEffect(
            component=component,
            variant_key=key,
            verdict=_VERDICTS[(comparison := compare(key)).verdict],
            comparison=comparison,
        )
        for component, key in spec.variant_keys.items()
    ]
    notes: list[str] = []
    if full_vs_minimal.n_tasks < spec.min_tasks:
        notes.append(
            f"Only {full_vs_minimal.n_tasks} paired task(s); verdicts need at least {spec.min_tasks}. "
            "Repetitions reduce noise within a task but do not add tasks."
        )
    return AblationReport(
        bundle=spec.bundle,
        bundle_hash=spec.bundle_hash,
        base_variant=spec.base_variant,
        full_vs_minimal=full_vs_minimal,
        full_verdict=_VERDICTS[full_vs_minimal.verdict],
        components=effects,
        notes=notes,
    )


def report_for_experiment(exp_row: Any) -> AblationReport | None:
    """Recompute the ablation report of a stored experiment (None if it was not an ablation)."""
    data = (exp_row.spec_json or {}).get("ablation")
    if not data:
        return None
    spec = AblationSpec(**data)
    samples = samples_from_rows(
        exp_row.runs, {t.id: t for t in exp_row.tasks}, {v.id: v for v in exp_row.variants}
    )
    return analyze_ablation(spec, samples, [t.task_key for t in exp_row.tasks])
