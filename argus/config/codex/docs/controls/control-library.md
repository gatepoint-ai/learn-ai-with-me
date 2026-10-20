# Revenue Assurance Control Library

Version 1.3 — fictional demo content for Argus. Every control has an ID, an owner, a frequency and
the Sentry probe that implements it. When a probe fires, the "When it fires" section is the first
response, not a full investigation.

## CTL-REV-001 Feed completeness

**Objective.** Every site's usage feed arrives complete each day. A partial feed means usage that is
never rated, never billed and never collected — revenue that disappears without anyone deciding to
lose it.

**Implemented by.** Probe `PR-FEED-COMPLETENESS` on the `site_volume_baseline` lens. It compares each
site's daily event count with that site's own trailing mean and fires when volume is more than 60%
below normal, provided the baseline has at least seven days of history.

**Owner.** Data operations. **Frequency.** Daily, after the overnight load.

**When it fires.** Check the ingestion log for the site and day first. If the source system holds
more events than were loaded, request a replay of the missing window before the billing cut-off.
Do not adjust the baseline to make the alert go away.

**Systemic case.** If at least half of all sites breach on the same day, the alerts are suppressed
as systemic. That pattern is a public holiday, a regional outage or an upstream platform failure —
one event with one owner, not a fault at each site. Data operations still confirms the cause.

## CTL-REV-002 Duplicate ingestion

**Objective.** No usage event is counted twice. Duplicated events inflate billed revenue, generate
customer disputes and later reverse as credits.

**Implemented by.** Probe `PR-VOLUME-SPIKE` on the `site_volume_baseline` lens. It fires when a
site's daily volume is more than 60% above its trailing mean.

**Owner.** Data operations. **Frequency.** Daily.

**When it fires.** Compare event IDs for the day against the previous load. A spike with repeated
event IDs is a replay; a spike with unique IDs may be genuine demand and must be confirmed with the
site before any events are removed.

## CTL-REV-003 Rating accuracy

**Objective.** Every event is billed at the rate its tariff defines. A rating error is the most
expensive kind of leakage because it is systematic: it repeats on every event until someone
notices.

**Implemented by.** Probe `PR-RATING-ACCURACY` on the `rating_quality` lens. It fires when the share
of mis-rated events for a customer class rises more than 15 percentage points above its 14-day
trailing rate.

**Owner.** Billing operations. **Frequency.** Daily.

**When it fires.** Treat it as a possible rating-engine regression. Identify the first affected
day, check whether a tariff or rating-rule change was deployed that day (see the rating change
procedure, RCP-4), and stop the change if it is still live. Re-rating affected events requires
approval under policy WO-POL-3.

## CTL-REV-004 Write-off drift

**Objective.** A site writes off revenue at its normal rate. A sudden rise means a collection
process has broken — a payment channel down, a dunning run skipped, or accounts closed in error.

**Implemented by.** Probe `PR-COLLECTION-DRIFT` on the `collection_mix` lens. It fires when a
site's write-off share of expected revenue rises more than 10 percentage points above its 14-day
trailing share.

**Owner.** Credit control. **Frequency.** Daily.

**When it fires.** Pull the list of accounts written off at the site on the affected days. Write-offs
above the approval limit in WO-POL-2 must have an approver recorded; if they do not, escalate under
ESC-1 the same day.

## CTL-REV-005 Unbilled usage

**Objective.** Every rated event appears on an invoice. Unbilled usage is revenue the business has
already delivered and simply never asked to be paid for.

**Implemented by.** KPI `unbilled_rate` in Pulse, with a warning above 3% and critical above 4% of
events. There is no per-site probe yet; this control is monitored at network level.

**Owner.** Billing operations. **Frequency.** Weekly review.

**When it fires.** Reconcile rated events against invoice lines for the period and list the
unmatched events by product. A cluster in one product usually means a billing-system mapping is
missing.

## CTL-REV-006 Leakage rate review

**Objective.** The overall gap between expected and collected revenue stays within its normal band,
and every movement outside the band is explained.

**Implemented by.** KPI `leakage_rate` in Pulse, with a warning at 19% and critical at 22% of
expected revenue. The rate includes pending and disputed revenue that may still be collected, so
the normal level is well above zero.

**Owner.** Head of revenue assurance. **Frequency.** Weekly, and monthly to the finance committee.

**When it fires.** Break the rate down by site, customer class and channel. A movement concentrated
in one dimension points to one process; a broad-based movement points to a change in customer
behaviour or the economy and is reported, not investigated as a fault.
