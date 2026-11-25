"""Lesson 11 - verified means reconciled, not "the script exited zero".

Compares what a database holds today with what a backup claims to contain,
object by object, and refuses to say "verified" until every difference has a
written explanation.

It also shows a quieter trap: picking "the latest backup" by sorting names.
Alphabetical order is not date order, and the script still reports success.

Standard library only. Made-up manifests. Run:  python reconcile.py
"""

from __future__ import annotations

# --- What production holds, and what the new backup contains (name -> size in MB)
PRODUCTION = {
    "billing.invoices": 4_200, "billing.invoice_lines": 9_800, "billing.customers": 310,
    "billing.usage_2026_09": 2_100, "billing.usage_2026_10": 2_300,
    "reports.monthly_summary": 0,          # a derived view, emptied by a housekeeping job
}
BACKUP = {
    "billing.invoices": 4_200, "billing.invoice_lines": 9_800, "billing.customers": 310,
    "billing.usage_2026_09": 2_100, "billing.usage_2026_10": 2_300,
    "reports.monthly_summary": 0,
}
PREVIOUS_BACKUP = {
    "billing.invoices": 4_100, "billing.invoice_lines": 9_600, "billing.customers": 305,
    "billing.usage_2026_09": 2_100,
    "reports.monthly_summary": 4_000,
}

# --- Explanations someone has investigated and written down -----------------
EXPLAINED = {
    "billing.usage_2026_10": "new monthly partition, created after the previous backup",
    "reports.monthly_summary": "derived view; contents can be rebuilt after restore, not data loss",
}


def reconcile(production: dict, backup: dict) -> list[str]:
    problems = []
    for name in sorted(set(production) | set(backup)):
        if name not in backup:
            problems.append(f"MISSING from backup: {name}")
        elif name not in production:
            problems.append(f"EXTRA in backup (not in production): {name}")
        elif production[name] != backup[name]:
            problems.append(f"SIZE differs: {name} production {production[name]:,} MB vs backup {backup[name]:,} MB")
    return problems


def explain_change(previous: dict, current: dict, notes: dict) -> tuple[list[str], list[str]]:
    """Why is this backup different from the last one? Every change needs a reason."""
    explained, unexplained = [], []
    for name in sorted(set(previous) | set(current)):
        before, after = previous.get(name), current.get(name)
        if before == after:
            continue
        line = f"{name}: {before or 0:,} -> {after or 0:,} MB"
        if name in notes:
            explained.append(f"{line}  ({notes[name]})")
        elif before is not None and after is not None and after > before:
            explained.append(f"{line}  (normal growth)")
        else:
            unexplained.append(line)
    return explained, unexplained


def pick_latest_backup_by_name(labels: list[str]) -> str:
    return sorted(labels)[-1]  # what `ls | tail -1` does


def main() -> None:
    print("=" * 72)
    print("Step 1: the backup job")
    print("=" * 72)
    print("exit code 0, log says 'completed'.  This proves the job ended. Nothing more.\n")

    print("=" * 72)
    print("Step 2: reconcile production against the backup, object by object")
    print("=" * 72)
    problems = reconcile(PRODUCTION, BACKUP)
    print(f"objects in production: {len(PRODUCTION)}   objects in backup: {len(BACKUP)}")
    print("\n".join(problems) if problems else "every object present, every size matches")

    print("\n" + "=" * 72)
    print("Step 3: the new backup is SMALLER than last month's. Stop and explain it")
    print("=" * 72)
    print(f"previous: {sum(PREVIOUS_BACKUP.values()):,} MB in {len(PREVIOUS_BACKUP)} objects")
    print(f"new:      {sum(BACKUP.values()):,} MB in {len(BACKUP)} objects   <- smaller, yet more objects")
    for notes, when in (({}, "before anyone investigates"), (EXPLAINED, "after investigating")):
        explained, unexplained = explain_change(PREVIOUS_BACKUP, BACKUP, notes)
        print(f"\n  {when}:")
        for line in explained:
            print("    explained   ", line)
        for line in unexplained:
            print("    UNEXPLAINED ", line)
        verified = not problems and not unexplained
        print(f"  VERIFIED: {'yes' if verified else 'NO - every difference needs a written explanation first'}")

    print("\n" + "=" * 72)
    print("The quiet trap: which backup is 'the latest'?")
    print("=" * 72)
    labels = ["backup-22sep", "backup-20261027"]
    print(f"on disk: {labels}  (22 Sep is last month's rollback copy; 20261027 is today's)")
    print(f"sorted by name, the script picks: {pick_latest_backup_by_name(labels)}")
    print("It would upload and 'verify' last month's backup, and report success.")
    print("Fix: pick by date, never by name, and print which file you picked.")


if __name__ == "__main__":
    main()
