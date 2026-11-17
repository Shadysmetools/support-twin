# Odoo Runbook (synthetic test copy)

## Entries

### Stray IN line (orphan product on a receipt: demand 0, done 0, no PO line)
- **Signature:** A receipt transfer cannot be validated without a Purchase Order reference
- **Severity / impact:** Blocks the wave.
- **Root cause:** A zero-demand line with no PO line was added by hand.
- **Diagnose:** Read the moves on the receipt.
- **Fix:** Remove the single stray move after the guarded check.
- **First seen:** 2026-07-01 · **Source:** Telegram · **Status:** confirmed 2026-07-26
- **Tags:** inventory, receipts, stray-in

### Phantom reservation after a cancelled put-away
- **Signature:** On hand 0 but reserved above 0 at an internal location
- **Root cause:** Cancelled transfer left a reservation.
- **Fix:** Release the reservation from Inventory > Reporting > Locations.
- **First seen:** 2026-08-01 · **Source:** email · **Status:** provisional (drafted, unconfirmed)
- **Tags:** inventory, reservation
