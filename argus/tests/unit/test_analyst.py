"""Tests for the Analyst's guard rails -- the number check and tool middleware.

The language model itself is exercised against the running stack. These pin
down what does not depend on it: a figure the tools never returned is caught,
presentation (rounding, percent, typography) is not mistaken for invention,
and oversized tool results are cut down before they reach the model.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "libs" / "argus-core"))
sys.path.insert(0, str(REPO_ROOT / "services" / "analyst"))

from analyst import tools, verify  # noqa: E402

ALERTS = {"count": 2, "rows": [
    {"subject_id": "SITE-03", "period": "2026-10-01",
     "summary": "Feed completeness: SITE-03 on 2026-10-01 -- volume -85%, below the limit of -60%."},
    {"subject_id": "ENTERPRISE", "period": "2026-10-11", "observed_value": 0.552},
]}


def test_figures_quoted_from_tools_are_verified():
    answer = "SITE-03 fell 85% on 2026-10-01; ENTERPRISE mis-rating hit 55.2%."
    result = verify.check(answer, [ALERTS])
    assert result.ok, result.unverified
    assert set(result.verified) == {"85%", "55.2%"}


def test_a_figure_the_model_worked_out_itself_is_caught():
    """85 + 55.2 = 140.2 appears nowhere in the tool results."""
    result = verify.check("Together that is 140.2% of normal.", [ALERTS])
    assert not result.ok
    assert result.unverified == ["140.2%"]


def test_ids_dates_and_small_counts_are_not_figures():
    text = "SITE-03, CTL-REV-004 and WO-POL-2 on 2026-10-13, 1 Oct: 3 alerts over 2 days."
    assert verify.figures(text) == []


def test_typographic_dashes_and_spaces_do_not_break_dates():
    """Regression: a model wrote 2026‑10‑13 with non-breaking hyphens."""
    assert verify.figures("On 2026‑10‑13 the share was 52.8 %.")[0].text == "52.8 %"
    assert len(verify.figures("On 2026‑10‑13 the share was 52.8 %.")) == 1


def test_rounding_is_presentation_not_invention():
    payload = [{"value": 16.4412}]
    assert verify.check("Leakage was 16.44%.", payload).ok
    assert verify.check("Leakage was about 16%.", payload).ok
    assert not verify.check("Leakage was 16.5%.", payload).ok


def test_thousands_separators_match():
    assert verify.check("Collected 11,920.94 yesterday.", [{"value": 11920.94}]).ok


def test_large_results_are_truncated_before_the_model_sees_them():
    rows = [{"i": i} for i in range(500)]
    out = tools.truncate({"rows": rows, "row_count": 500})
    assert len(out["rows"]) == tools.MAX_ROWS
    assert out["rows_omitted"] == 500 - tools.MAX_ROWS


def test_every_denial_names_the_failure_it_prevents():
    assert tools.DENIED
    for d in tools.DENIED:
        assert d["capability"] and len(d["prevents"]) > 20


def test_a_figure_from_the_question_is_not_flagged():
    """Regression: "approve a write-off of 7,000" -- the model repeated the user's figure."""
    question = "Who has to approve a write-off of 7,000?"
    answer = "Above 5,000, so 7,000 needs the head of credit control."
    assert verify.check(answer, [question, {"text": "Above 5,000: the head of credit control"}]).ok


def test_flat_query_arguments_become_a_prism_query():
    box = tools.Toolbox.__new__(tools.Toolbox)
    sent = {}
    box._post = lambda svc, path, body: sent.update(body) or {"rows": [], "row_count": 0}
    box._query_lens({"lens": "usage_trends", "fields": ["site_id"], "order_by": "-site_id"})
    assert sent["fields"] == [{"field": "site_id"}]
    assert sent["order_by"] == [{"field": "site_id", "direction": "desc"}]


def test_a_day_range_is_a_date_not_a_figure():
    """Regression: "the affected days (16–19 Oct)" was flagged as the figure 16."""
    assert verify.figures("Pull the accounts for the affected days (16–19 Oct).") == []
    assert verify.figures("between 3 to 5 Nov") == []
