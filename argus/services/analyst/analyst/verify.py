"""The number check: every figure in an answer must come from a tool result.

The platform's first rule is that the language model never calculates. A
prompt can ask for that; only a check can confirm it. This module extracts
the figures from the model's answer and looks for each one in the data the
tools actually returned. A figure with no source is reported as unverified --
and the answer is labelled, not silently shown.

Matching allows for presentation, not for arithmetic: 0.8494 may appear as
"85%", 16.4412 as "16.44", -0.85 as "85% below". A sum, difference or average
the model worked out itself will not match, which is the point.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# Tokens that contain digits but are names, not figures: SITE-03, CTL-REV-004,
# PR-FEED-COMPLETENESS, WO-POL-2, ACCT-001234 -- and ISO dates.
_IDENTIFIER = re.compile(r"\b[A-Za-z]+(?:[-_][A-Za-z0-9]+)*[-_]\d+[A-Za-z0-9]*\b")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MONTH_DAY = re.compile(
    # "16-19 Oct" and "16 to 19 Oct" are one date range, not the figure 16.
    r"\b\d{1,2}\s*(?:-|to)\s*\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b"
    r"|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?(?:\s?%)?")

#: Small whole numbers ("3 days", "top 5", "2 alerts") are too common to check
#: meaningfully and are skipped. Anything with decimals or a % sign is checked.
SMALL_INT_LIMIT = 12


@dataclass(frozen=True)
class Figure:
    text: str
    value: float
    decimals: int
    percent: bool


@dataclass
class NumberCheck:
    checked: int = 0
    verified: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unverified

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checked": self.checked, "verified": self.verified,
                "unverified": self.unverified}


#: Models often typeset with non-breaking hyphens, en dashes and thin spaces.
#: "2026‑10‑13" must still be recognised as a date, "−" as a minus.
_TYPOGRAPHY = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
})


def normalise(text: str) -> str:
    return text.translate(_TYPOGRAPHY)


def figures(text: str) -> list[Figure]:
    """The figures in an answer, with identifiers and dates removed first."""
    cleaned = _IDENTIFIER.sub(" ", normalise(text))
    cleaned = _ISO_DATE.sub(" ", cleaned)
    cleaned = _MONTH_DAY.sub(" ", cleaned)
    out = []
    for m in _NUMBER.finditer(cleaned):
        raw = m.group(0).strip()
        percent = raw.endswith("%")
        number = raw.rstrip("%").strip().replace(",", "")
        try:
            value = float(number)
        except ValueError:
            continue
        decimals = len(number.split(".")[1]) if "." in number else 0
        if not percent and decimals == 0 and abs(value) <= SMALL_INT_LIMIT:
            continue
        out.append(Figure(text=raw, value=value, decimals=decimals, percent=percent))
    return out


def source_numbers(payloads: list[Any]) -> list[float]:
    """Every number in the tool results: JSON numbers and figures inside strings."""
    found: list[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            found.append(float(node))
        elif isinstance(node, str):
            for f in figures(node):
                found.append(f.value)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                walk(v)

    for p in payloads:
        walk(json.loads(p) if isinstance(p, str) and p[:1] in "[{" else p)
    return found


def _matches(figure: Figure, source: float) -> bool:
    tolerance = 0.5 * 10 ** (-figure.decimals) + 1e-9
    candidates = {source, abs(source)}
    if figure.percent:
        candidates |= {source * 100, abs(source) * 100}
    target = abs(figure.value)
    return any(abs(abs(c) - target) <= tolerance for c in candidates)


def check(answer: str, payloads: list[Any]) -> NumberCheck:
    sources = source_numbers(payloads)
    result = NumberCheck()
    for f in figures(answer):
        result.checked += 1
        if any(_matches(f, s) for s in sources):
            result.verified.append(f.text)
        else:
            result.unverified.append(f.text)
    return result
