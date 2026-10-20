"""Tests for the dashboard's page config -- `config/deck/views/`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "libs" / "argus-core"))
sys.path.insert(0, str(REPO_ROOT / "services" / "deck"))

from argus_core.config import ConfigError, load_kpis  # noqa: E402
from deck.views import load_views  # noqa: E402


def test_shipped_views_load_and_name_real_kpis():
    kpis = set(load_kpis(REPO_ROOT / "config" / "kpis"))
    views = load_views(REPO_ROOT / "config" / "deck" / "views", known_kpis=kpis)
    assert [v.path for v in views] == ["/", "/alerts", "/ask"]


def test_a_chart_for_an_unknown_kpi_is_refused(tmp_path):
    (tmp_path / "v.yaml").write_text(
        "id: x\ntitle: X\npath: /x\npanels:\n  - {type: kpi_chart, title: T, kpi: nope}\n")
    with pytest.raises(ConfigError, match="unknown kpi"):
        load_views(tmp_path, known_kpis={"leakage_rate"})


def test_an_unknown_panel_type_is_refused(tmp_path):
    (tmp_path / "v.yaml").write_text("id: x\ntitle: X\npath: /x\npanels:\n  - {type: pie, title: T}\n")
    with pytest.raises(ConfigError):
        load_views(tmp_path)
