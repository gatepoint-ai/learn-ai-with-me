"""Dashboard page definitions -- the YAML contract in `config/deck/views/`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from argus_core.config import ConfigError


class Panel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["kpi_tiles", "kpi_chart", "alerts_table", "ask"]
    title: str
    kpi: str | None = None
    status: Literal["open", "suppressed", "closed", "all"] = "open"
    limit: int = Field(default=50, ge=1, le=500)
    examples: list[str] = Field(default_factory=list)


class View(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    title: str
    path: str = Field(pattern=r"^/[a-z0-9/-]*$")
    description: str = ""
    panels: list[Panel]


def load_views(directory: str | Path, known_kpis: set[str] | None = None) -> list[View]:
    """Load every view, refusing the whole set if any file is wrong."""
    problems: list[str] = []
    views: list[View] = []
    for path in sorted(Path(directory).glob("*.yaml")):
        try:
            view = View.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (ValidationError, yaml.YAMLError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        for panel in view.panels:
            if panel.type == "kpi_chart" and not panel.kpi:
                problems.append(f"{path.name}: kpi_chart '{panel.title}' names no kpi")
            if known_kpis is not None and panel.kpi and panel.kpi not in known_kpis:
                problems.append(f"{path.name}: unknown kpi {panel.kpi!r}")
        views.append(view)
    paths = [v.path for v in views]
    if len(paths) != len(set(paths)):
        problems.append("two views share a path")
    if not views and not problems:
        problems.append(f"no views in {directory}")
    if problems:
        raise ConfigError(problems)
    return views
