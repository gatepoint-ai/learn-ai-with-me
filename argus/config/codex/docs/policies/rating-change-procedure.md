# Rating Change Procedure (RCP)

Fictional demo content for Argus. Every change to a tariff, a price or a rating rule follows this
procedure, because a rating error repeats on every event until it is found.

## RCP-1 Request

A rating change is requested by the product owner with the tariff, the effective date, the customer
classes affected and the expected revenue impact.

## RCP-2 Test

Before deployment, billing operations rates a sample of real events from the previous month under
the new rule and compares the result with the expected impact. A difference of more than 2% from
the expected impact stops the change.

## RCP-3 Deploy

Changes are deployed on weekdays only, never on the last three days of a billing month, so that a
fault is found before invoices are produced.

## RCP-4 Watch

For seven days after deployment, billing operations reviews the rating accuracy control
(CTL-REV-003) daily for the affected customer classes. If the mis-rating share rises, the change is
rolled back first and investigated second.

## RCP-5 Record

Every change, test result, deployment and rollback is logged with its date and owner. When the
rating accuracy control fires, this log is the first place to look.
