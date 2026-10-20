# Collections Procedure (COL-PROC)

Fictional demo content for Argus. How an unpaid invoice moves from due to collected, or to
write-off.

## Collection stages

An unpaid amount moves through five stages. Each stage recovers less than the one before, which is
why the early stages matter most.

1. **Automatic payment** — direct debit or stored card on the due date.
2. **Reminder** — an automated reminder 3 days after the due date.
3. **Dunning** — two dunning letters, at 14 and 28 days, the second warning of service suspension.
4. **Suspension and contact** — service suspended at 35 days; an agent contacts the customer.
5. **External recovery or write-off** — after 90 days the debt goes to an external agency or is
   written off under the write-off policy (WO-POL).

## Dunning runs

Dunning letters are produced by a nightly batch. If the batch fails, nobody is told by default:
the letters simply do not go out, and payments that would have come in a week later never arrive.
Credit control checks the batch report every morning. A skipped run must be re-run the same day.

## Payment channels

Customers pay by direct debit, card, bank transfer or cash at a site. Each site's payment terminal
reports to the payments platform hourly. A site whose terminal stops reporting will show collected
revenue falling and pending revenue rising for that site only.

## Promises to pay

An agent may agree a promise to pay of up to 30 days. A broken promise moves the account straight to
the next stage. Agents may not agree more than two promises per account in a year.
