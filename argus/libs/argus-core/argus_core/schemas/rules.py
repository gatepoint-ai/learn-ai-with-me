"""Rule set -- the YAML contract in `config/rules/`.

A rule owns one or more probes. Each probe is a threshold test over a Prism
lens query, and a breach becomes an alert attributed to a subject (a site, a
partner, a channel -- whatever the probe declares).

Rules live in config rather than code so that adding a check is adding a file.
The engine has no rule-specific branches.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .query import IDENTIFIER_PATTERN, QueryRequest


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Comparator(StrEnum):
    """How the measured value is tested against the threshold."""

    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    PCT_CHANGE_ABOVE = "pct_change_above"
    PCT_CHANGE_BELOW = "pct_change_below"
    PP_DRIFT_ABOVE = "pp_drift_above"

    @property
    def needs_baseline(self) -> bool:
        return self in {
            Comparator.PCT_CHANGE_ABOVE,
            Comparator.PCT_CHANGE_BELOW,
            Comparator.PP_DRIFT_ABOVE,
        }


class SubjectType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=IDENTIFIER_PATTERN)
    description: str = ""


class AlertType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    description: str = ""


class Probe(BaseModel):
    """A single executable check."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
    name: str
    description: str = ""
    lens: str = Field(pattern=IDENTIFIER_PATTERN)
    query: QueryRequest
    value_field: str = Field(pattern=IDENTIFIER_PATTERN)
    baseline_field: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    subject_field: str = Field(pattern=IDENTIFIER_PATTERN)
    subject_type: str = Field(pattern=IDENTIFIER_PATTERN)
    comparator: Comparator
    threshold: float
    severity: Severity = Severity.MEDIUM
    alert_type: int
    evidence_fields: list[str] = Field(default_factory=list)
    active: bool = True

    def model_post_init(self, _context: object) -> None:
        if self.comparator.needs_baseline and self.baseline_field is None:
            raise ValueError(
                f"probe {self.id}: comparator {self.comparator} "
                "requires baseline_field"
            )


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str = ""
    process: str = ""
    owner: str = ""
    active: bool = True
    probes: list[Probe]


class RuleSet(BaseModel):
    """One `config/rules/<name>.yaml` file."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    subject_types: list[SubjectType] = Field(default_factory=list)
    alert_types: list[AlertType] = Field(default_factory=list)
    rules: list[Rule]

    @property
    def all_probes(self) -> list[Probe]:
        """Active probes from active rules, in file order."""
        return [
            probe
            for rule in self.rules
            if rule.active
            for probe in rule.probes
            if probe.active
        ]
