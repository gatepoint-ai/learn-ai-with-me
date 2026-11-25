"""Lesson 13 - autonomy is earned, not granted.

An AI agent drafts replies to customer correspondence. How much it may do on
its own is not decided up front. Each kind of request (a "scenario") climbs a
ladder, one stage at a time, on evidence the reviewers create just by doing
their jobs: how often they sent the agent's draft without changing anything.

  Stage 1  SUGGEST   the agent writes a checklist; a person does the work
  Stage 2  DRAFT     the agent does the work; a person reviews every one
  Stage 3  AUTO      review switched off; a person spot-checks

Each scenario has its own threshold, and there is a path back down.

Standard library only. Made-up review history. Run:  python autonomy_ladder.py
"""

from __future__ import annotations

STAGES = ["SUGGEST", "DRAFT", "AUTO"]

# Per scenario: the share of drafts sent unchanged needed to climb, and the
# minimum number of reviewed drafts before any decision. Riskier work, higher bar.
POLICY = {
    "address change":      {"promote_at": 0.90, "min_reviews": 40},
    "copy of an invoice":  {"promote_at": 0.90, "min_reviews": 40},
    "payment dispute":     {"promote_at": 0.98, "min_reviews": 100},
    "credit note request": {"promote_at": None, "min_reviews": 0},   # never leaves human review
}
DEMOTE_BELOW = 0.80  # an AUTO scenario whose spot-checks fall below this goes back to DRAFT

# Review history: (scenario, current stage, drafts reviewed, sent unchanged)
HISTORY = [
    ("address change",      "DRAFT", 120, 116),
    ("copy of an invoice",  "DRAFT",  25,  25),
    ("payment dispute",     "DRAFT", 150, 139),
    ("credit note request", "DRAFT", 300, 296),
    ("address change",      "AUTO",   50,  37),   # a later month: spot-checks after a form change
]


def decide(scenario: str, stage: str, reviewed: int, unchanged: int) -> tuple[str, str]:
    policy = POLICY[scenario]
    rate = unchanged / reviewed if reviewed else 0.0
    if stage == "AUTO":
        if rate < DEMOTE_BELOW:
            return "DRAFT", f"spot-checks unchanged {rate:.0%} < {DEMOTE_BELOW:.0%}: back to human review"
        return "AUTO", f"spot-checks unchanged {rate:.0%}: stays automatic"
    if policy["promote_at"] is None:
        return stage, "policy: this scenario always keeps a human reviewer, whatever the rate"
    if reviewed < policy["min_reviews"]:
        return stage, f"only {reviewed} reviewed, need {policy['min_reviews']} before deciding (rate so far {rate:.0%})"
    if rate >= policy["promote_at"]:
        return STAGES[STAGES.index(stage) + 1], f"unchanged {rate:.0%} >= {policy['promote_at']:.0%}: promoted"
    return stage, f"unchanged {rate:.0%} < {policy['promote_at']:.0%}: stays, keep reviewing"


def main() -> None:
    print(f"{'Scenario':<22} {'Now':<8} {'Next':<8} Reason")
    print("-" * 100)
    for scenario, stage, reviewed, unchanged in HISTORY:
        next_stage, reason = decide(scenario, stage, reviewed, unchanged)
        print(f"{scenario:<22} {stage:<8} {next_stage:<8} {reason}")
    print("\nThe metric is not the model's confidence or a test-set score.")
    print("It is whether the people who know the job changed anything before sending it.")


if __name__ == "__main__":
    main()
