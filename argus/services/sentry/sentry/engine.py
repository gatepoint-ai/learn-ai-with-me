"""Sentry's judgement -- pure functions, no network, no database.

A probe's rows come back from Prism; this module decides which rows breach,
which breaches are really one systemic event, and what the alert says. Every
decision is deterministic and explainable without a language model: the alert
text is a template filled with the numbers that triggered it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from argus_core.schemas.rules import Comparator, Probe


@dataclass(frozen=True)
class Breach:
    probe_id: str
    subject_type: str
    subject_id: str
    period: date
    observed: float
    baseline: float | None
    deviation: float | None
    severity: str
    alert_type: int
    threshold: float
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    status: str = "open"  # open | suppressed


@dataclass(frozen=True)
class Evaluation:
    breaches: list[Breach]
    rows_examined: int
    #: Every (subject, period) the probe looked at -- the scope for auto-close.
    examined: set[tuple[str, date]]


# --------------------------------------------------------------------------
# Config checks
# --------------------------------------------------------------------------


def check_probe(probe: Probe) -> None:
    """The probe's query must return every field the engine reads."""
    selected = {f.field for f in probe.query.fields}
    needed = {probe.value_field, probe.subject_field, probe.period_field}
    if probe.baseline_field:
        needed.add(probe.baseline_field)
    needed.update(probe.evidence_fields)
    missing = sorted(needed - selected)
    if missing:
        raise ValueError(f"probe {probe.id}: query does not select {', '.join(missing)}")
    if probe.query.aggregates:
        raise ValueError(f"probe {probe.id}: probes read rows, not aggregates")


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def breaches(comparator: Comparator, value: float, baseline: float | None, threshold: float) -> tuple[bool, float | None]:
    """Return (breached, deviation)."""
    if comparator is Comparator.GT:
        return value > threshold, value - threshold
    if comparator is Comparator.GTE:
        return value >= threshold, value - threshold
    if comparator is Comparator.LT:
        return value < threshold, value - threshold
    if comparator is Comparator.LTE:
        return value <= threshold, value - threshold
    if baseline is None:
        return False, None
    if comparator is Comparator.PP_DRIFT_ABOVE:
        drift = value - baseline
        return drift > threshold, drift
    if baseline == 0:
        return False, None
    change = (value - baseline) / abs(baseline)
    if comparator is Comparator.PCT_CHANGE_ABOVE:
        return change > threshold, change
    if comparator is Comparator.PCT_CHANGE_BELOW:
        return change < -threshold, change
    raise ValueError(f"unhandled comparator {comparator}")  # pragma: no cover


def evaluate(probe: Probe, rows: list[dict[str, Any]]) -> Evaluation:
    found: list[Breach] = []
    examined: set[tuple[str, date]] = set()
    for row in rows:
        value = row.get(probe.value_field)
        if value is None:
            continue
        subject = str(row[probe.subject_field])
        period = _as_date(row[probe.period_field])
        examined.add((subject, period))
        baseline = row.get(probe.baseline_field) if probe.baseline_field else None
        hit, deviation = breaches(
            probe.comparator, float(value), None if baseline is None else float(baseline),
            probe.threshold,
        )
        if not hit:
            continue
        found.append(
            Breach(
                probe_id=probe.id,
                subject_type=probe.subject_type,
                subject_id=subject,
                period=period,
                observed=float(value),
                baseline=None if baseline is None else float(baseline),
                deviation=deviation,
                severity=probe.severity.value,
                alert_type=probe.alert_type,
                threshold=probe.threshold,
                summary=summarise(probe, subject, period, float(value),
                                  None if baseline is None else float(baseline)),
                evidence={f: row.get(f) for f in probe.evidence_fields},
            )
        )
    found = suppress_systemic(probe, found, examined)
    return Evaluation(breaches=found, rows_examined=len(rows), examined=examined)


def suppress_systemic(
    probe: Probe, found: list[Breach], examined: set[tuple[str, date]]
) -> list[Breach]:
    """Mark breaches suppressed when most subjects breach on the same day.

    Eight sites dropping 70% on one day is a holiday or an upstream outage --
    one event, not eight faults, and not something a site manager can fix.
    The alerts are kept (status `suppressed`, with the reason) so the decision
    is auditable, but they do not count as open problems.
    """
    if probe.suppress_when_share is None or not found:
        return found
    subjects_per_period: dict[date, int] = defaultdict(int)
    for _, period in examined:
        subjects_per_period[period] += 1
    breaching: dict[date, int] = defaultdict(int)
    for b in found:
        breaching[b.period] += 1

    out = []
    for b in found:
        total = subjects_per_period[b.period]
        hit = breaching[b.period]
        if hit >= probe.suppress_min_subjects and hit / total >= probe.suppress_when_share:
            reason = (
                f"Suppressed as systemic: {hit} of {total} {probe.subject_type}s "
                f"breached on {b.period.isoformat()}, which points to a shared cause "
                f"rather than a {probe.subject_type} fault."
            )
            out.append(
                Breach(**{**b.__dict__, "status": "suppressed",
                          "summary": f"{b.summary} {reason}",
                          "evidence": {**b.evidence, "suppressed_reason": reason}})
            )
        else:
            out.append(b)
    return out


# --------------------------------------------------------------------------
# Wording
# --------------------------------------------------------------------------


def summarise(probe: Probe, subject: str, period: date, value: float, baseline: float | None) -> str:
    """One deterministic sentence per alert -- the numbers that triggered it."""
    day = period.isoformat()
    c = probe.comparator
    what = probe.value_label or probe.value_field.replace("_", " ")
    if c in (Comparator.GT, Comparator.GTE, Comparator.LT, Comparator.LTE):
        word = "above" if c in (Comparator.GT, Comparator.GTE) else "below"
        return (
            f"{probe.name}: {subject} on {day} -- {what} {_fmt(value)}, "
            f"{word} the limit of {_fmt(probe.threshold)}."
        )
    if c is Comparator.PP_DRIFT_ABOVE:
        return (
            f"{probe.name}: {subject} on {day} -- {what} {_pct(value)} against a trailing "
            f"{_pct(baseline)}, up {_pp(value - baseline)} (limit {_pp(probe.threshold)})."
        )
    change = (value - baseline) / abs(baseline)
    return (
        f"{probe.name}: {subject} on {day} -- {what} changed {change:+.0%} from its trailing "
        f"{_fmt(baseline)} (limit {probe.threshold:.0%})."
    )


def _fmt(v: float) -> str:
    return f"{v:+.0%}" if -5 < v < 5 else f"{v:,.2f}"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1%}"


def _pp(v: float) -> str:
    return f"{v * 100:.1f} percentage points"


def _as_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
