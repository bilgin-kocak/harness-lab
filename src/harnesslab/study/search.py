"""Turn search stages into lists of configurations.

Stages are declared in the study spec (see :class:`harnesslab.study.spec.StageSpec`). Expansion is
deterministic: the same records and the same spec always give the same configurations, and the
evaluation order of a stage is a seeded shuffle, so a stage stopped by the budget has evaluated a
random subsample of its configurations rather than a biased prefix. The one exception is the
baseline: in a stage that contains it, it runs first, because it anchors cost caps and ratios and
without it nothing a stage evaluated could be compared or frozen.
"""

from __future__ import annotations

import hashlib
import itertools
import random
from collections.abc import Sequence
from typing import Any

from harnesslab.study.contract import Config, SearchSpace, canonical_json
from harnesslab.study.objective import Ranking, primary_score
from harnesslab.study.spec import ObjectiveSpec, StageSpec, StudySpecError


def _short(value: Any) -> str:
    text = canonical_json(value)
    return text.strip('"') if text.startswith('"') else text


def resolve_label(label: str, known: dict[str, Config]) -> Config:
    if label not in known:
        raise StudySpecError(f"unknown configuration label {label!r} (known: {', '.join(known)})")
    return known[label]


def explicit_configs(
    stage: StageSpec, space: SearchSpace, known: dict[str, Config]
) -> list[Config]:
    """The stage's configurations; each label is added to ``known``.

    A label may be declared again (the baseline in a smoke stage and in stage 1) only with the
    same settings: two different configurations under one name are a spec error.
    """
    out = []
    for c in stage.configs:
        base = resolve_label(c.base, known)
        cfg = base.replace(label=c.label, **c.set)
        space.validate_config(cfg)
        if c.label in known and known[c.label].config_hash != cfg.config_hash:
            raise StudySpecError(
                f"stage {stage.name!r}: label {c.label!r} is already defined with other settings"
            )
        known[c.label] = cfg
        out.append(cfg)
    return out


def stage_levels(stage: StageSpec, space: SearchSpace) -> list[list[Any]]:
    """The levels a grid or refine stage varies: the knob's, or the stage's subset of them."""
    out = []
    for k in stage.knobs:
        knob = space.knob(k)
        if k in stage.levels:
            wanted = stage.levels[k]
            unknown = [v for v in wanted if not knob.has_level(v)]
            if unknown:
                raise StudySpecError(
                    f"stage {stage.name!r}: {k} levels {unknown} are not in the search space"
                )
            out.append([v for v in knob.levels if any(_same(v, w) for w in wanted)])
        else:
            out.append(list(knob.levels))
    return out


def _same(a: Any, b: Any) -> bool:
    return canonical_json(a) == canonical_json(b)


def grid_configs(stage: StageSpec, space: SearchSpace, base: Config) -> list[Config]:
    levels = stage_levels(stage, space)
    out = []
    for combo in itertools.product(*levels):
        changes = dict(zip(stage.knobs, combo, strict=True))
        label = stage.name + ":" + ",".join(f"{k}={_short(v)}" for k, v in changes.items())
        cfg = base.replace(label=label, **changes)
        space.validate_config(cfg)
        out.append(cfg)
    return out


def top_configs(ranking: Ranking, k: int) -> list[Config]:
    return [
        Config(values=s.values, label=s.label) for s in ranking.scores if s.feasible and s.rank
    ][:k]


def refine_configs(stage: StageSpec, space: SearchSpace, ranking: Ranking) -> list[Config]:
    out = []
    levels = stage_levels(stage, space)
    for parent in top_configs(ranking, stage.top_k):
        for combo in itertools.product(*levels):
            changes = dict(zip(stage.knobs, combo, strict=True))
            suffix = ",".join(f"{k}={_short(v)}" for k, v in changes.items())
            cfg = parent.replace(
                label=f"{parent.label or parent.config_hash[:12]}+{suffix}", **changes
            )
            space.validate_config(cfg)
            out.append(cfg)
    return out


def combine_config(
    stage: StageSpec,
    space: SearchSpace,
    known: dict[str, Config],
    ranking: Ranking,
    obj: ObjectiveSpec,
) -> tuple[Config | None, str]:
    """``reference`` plus every single-knob addition that beat it by ``min_gain``; or why not.

    An addition is a feasible configuration that differs from the reference in exactly one knob,
    on which the reference still has the baseline's level: it adds that knob on top of the
    reference. A configuration that undoes or changes a knob the reference already moved (the
    baseline itself, when the reference is the baseline plus one knob) is not an addition. The
    combined configuration keeps every setting of the reference and sets each winning knob to
    its best-scoring level.
    """
    ref = resolve_label(stage.reference or "", known)
    base = resolve_label("baseline", known)
    ref_score = ranking.get(ref.config_hash)
    if ref_score is None or not ref_score.feasible:
        return None, f"reference {stage.reference!r} has no feasible dev result"
    ref_primary = primary_score(ref_score, obj)
    winners: dict[str, tuple[float, Any]] = {}
    for s in ranking.scores:
        if not s.feasible or s.config_hash == ref.config_hash:
            continue
        diff = [k for k in ref.values if not _same(s.values.get(k), ref.values[k])]
        if len(diff) != 1:
            continue
        knob = diff[0]
        if not _same(ref.values[knob], base.values.get(knob)):
            continue  # the reference moved this knob: undoing or changing it adds nothing
        p = primary_score(s, obj)
        if p is None or ref_primary is None or p - ref_primary < stage.min_gain - 1e-12:
            continue
        if knob not in winners or p > winners[knob][0]:
            winners[knob] = (p, s.values[knob])
    if len(winners) < stage.min_winners:
        return None, (
            f"{len(winners)} single-knob addition(s) beat {stage.reference!r} by at least "
            f"{stage.min_gain:g}; {stage.min_winners} needed"
        )
    changes = {k: v for k, (_, v) in sorted(winners.items())}
    cfg = ref.replace(
        label=f"{stage.name}:{stage.reference}+" + "+".join(sorted(changes)), **changes
    )
    space.validate_config(cfg)
    return cfg, f"combined {', '.join(sorted(changes))}"


def confirm_configs(stage: StageSpec, ranking: Ranking, baseline: Config) -> list[Config]:
    out = top_configs(ranking, stage.top_k)
    if stage.include_baseline and all(c.config_hash != baseline.config_hash for c in out):
        out.append(baseline)
    return out


def dedupe(configs: Sequence[Config]) -> list[Config]:
    seen: set[str] = set()
    out = []
    for c in configs:
        if c.config_hash not in seen:
            seen.add(c.config_hash)
            out.append(c)
    return out


def shuffled(configs: Sequence[Config], seed: int, stage_name: str) -> list[Config]:
    """A seeded order that depends on the stage but not on Python's hash randomisation."""
    salt = int(hashlib.sha256(f"{seed}:{stage_name}".encode()).hexdigest()[:16], 16)
    order = sorted(configs, key=lambda c: c.config_hash)
    random.Random(salt).shuffle(order)
    return order


def baseline_first(configs: Sequence[Config], baseline_hash: str) -> list[Config]:
    """The baseline (when the stage contains it) first, the others in their given order."""
    first = [c for c in configs if c.config_hash == baseline_hash]
    return first + [c for c in configs if c.config_hash != baseline_hash]
