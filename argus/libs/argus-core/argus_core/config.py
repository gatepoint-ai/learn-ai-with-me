"""Load and validate YAML config into typed specs.

Every service loads its config at startup and refuses to serve if any file is
invalid. That is deliberate: a typo in a lens field list should stop the
container, not surface as a confusing query error on a Tuesday.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from .schemas.rules import RuleSet
from .schemas.lens import LensSpec
from .schemas.knowledge import CollectionSpec
from .schemas.kpi import KpiSpec

T = TypeVar("T", bound=BaseModel)


class ConfigError(RuntimeError):
    """One or more config files failed to load or validate.

    Carries every failure rather than the first, so a misconfigured deploy
    reports all of its problems in one container log.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(f"{len(problems)} config problem(s):\n  - {joined}")


def _load_dir(directory: Path, model: type[T], key: str) -> dict[str, T]:
    """Parse every ``*.yaml`` in `directory` into `model`, keyed by `key`."""
    if not directory.is_dir():
        raise ConfigError([f"config directory not found: {directory}"])

    loaded: dict[str, T] = {}
    problems: list[str] = []

    # Sorted so load order -- and therefore error order -- is reproducible.
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: invalid YAML: {exc}")
            continue

        if raw is None:
            problems.append(f"{path.name}: file is empty")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{path.name}: expected a mapping at the top level")
            continue

        try:
            spec = model.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"]) or "<root>"
                problems.append(f"{path.name}: {loc}: {err['msg']}")
            continue

        identifier = getattr(spec, key)
        if identifier in loaded:
            problems.append(f"{path.name}: duplicate {key} {identifier!r}")
            continue
        loaded[identifier] = spec

    if problems:
        raise ConfigError(problems)
    if not loaded:
        raise ConfigError([f"no config files found in {directory}"])
    return loaded


def load_lenses(directory: str | Path) -> dict[str, LensSpec]:
    return _load_dir(Path(directory), LensSpec, "name")


def load_kpis(directory: str | Path) -> dict[str, KpiSpec]:
    return _load_dir(Path(directory), KpiSpec, "id")


def load_collections(directory: str | Path) -> dict[str, CollectionSpec]:
    return _load_dir(Path(directory), CollectionSpec, "name")


def load_rules(directory: str | Path) -> dict[str, RuleSet]:
    rulesets = _load_dir(Path(directory), RuleSet, "name")
    _check_probe_ids_unique(rulesets)
    return rulesets


def _check_probe_ids_unique(
    rulesets: dict[str, RuleSet],
) -> None:
    """Probe ids must be globally unique.

    Alerts are keyed by probe id, so a collision across two ruleset
    files would silently merge unrelated alerts.
    """
    seen: dict[str, str] = {}
    problems: list[str] = []
    for ruleset_name, ruleset in rulesets.items():
        for rule in ruleset.rules:
            for probe in rule.probes:
                if probe.id in seen:
                    problems.append(
                        f"probe id {probe.id!r} defined in both "
                        f"{seen[probe.id]!r} and {ruleset_name!r}"
                    )
                else:
                    seen[probe.id] = ruleset_name
    if problems:
        raise ConfigError(problems)


def validate_lens_references(
    lenses: dict[str, LensSpec],
    kpis: dict[str, KpiSpec] | None = None,
    rules: dict[str, RuleSet] | None = None,
) -> None:
    """Cross-check that KPIs and rules reference lenses and fields that exist.

    Without this a KPI naming a dropped column fails at recalculation time, hours
    after the deploy that broke it.
    """
    problems: list[str] = []

    for kpi_id, kpi in (kpis or {}).items():
        lens = lenses.get(kpi.lens)
        if lens is None:
            problems.append(f"kpi {kpi_id}: unknown lens {kpi.lens!r}")
            continue
        known = lens.field_map
        for name, label in [(kpi.date_field, "date_field")] + [
            (b, "breakdown") for b in kpi.breakdowns
        ]:
            if name not in known:
                problems.append(
                    f"kpi {kpi_id}: {label} {name!r} not in lens {kpi.lens!r}"
                )
        for name, label in [
            (kpi.measure.field, "measure.field"),
            (kpi.measure.denominator_field, "measure.denominator_field"),
        ]:
            if name is not None and name not in known:
                problems.append(
                    f"kpi {kpi_id}: {label} {name!r} not in lens {kpi.lens!r}"
                )

    for ruleset_name, ruleset in (rules or {}).items():
        subject_types = {s.id for s in ruleset.subject_types}
        alert_types = {a.id for a in ruleset.alert_types}
        for probe in ruleset.all_probes:
            lens = lenses.get(probe.lens)
            if lens is None:
                problems.append(f"probe {probe.id}: unknown lens {probe.lens!r}")
                continue
            known = lens.field_map
            candidates = [
                (probe.value_field, "value_field"),
                (probe.baseline_field, "baseline_field"),
                (probe.subject_field, "subject_field"),
            ] + [(f, "evidence_field") for f in probe.evidence_fields]
            for name, label in candidates:
                if name is not None and name not in known:
                    problems.append(
                        f"probe {probe.id}: {label} {name!r} "
                        f"not in lens {probe.lens!r}"
                    )
            if probe.subject_type not in subject_types:
                problems.append(
                    f"probe {probe.id}: subject_type "
                    f"{probe.subject_type!r} not declared in {ruleset_name!r}"
                )
            if probe.alert_type not in alert_types:
                problems.append(
                    f"probe {probe.id}: alert_type "
                    f"{probe.alert_type} not declared in {ruleset_name!r}"
                )

    if problems:
        raise ConfigError(problems)
