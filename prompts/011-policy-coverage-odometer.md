Read AGENTS.md and SPEC.md first. Steps 1–10 are done; keep all tests passing. This step adds the policy record and coverage checks, plus two capture details, matching the "1. Coverage and vehicle" section of the Review card in `design-ref/claim.png` and `design-ref/claim-damage-diagram.png`. Write new code from the screenshots and this description.

## 1. Policy record

- New table `policies`: policy number, insured name, vehicle year/make/model/color, VIN, plate + state, coverages (collision: bool, comprehensive: bool, liability_only: bool), deductible (USD int), lienholder (text or null), effective date, expiry date.
- Seed four demo policies: `DEMO-AUTO-1001` (2020 Toyota Camry, blue, plate 7XYZ123 CA, VIN 1HGCM82633A004352, collision + comprehensive, $500 deductible, active), `DEMO-AUTO-1002` (2019 Honda Civic, white, 8KLM204 CA, collision + comprehensive, $1000, active), `DEMO-AUTO-1003` (2018 Ford F-150, black, 6TRK889 CA, liability only, active), `DEMO-AUTO-1004` (2017 Nissan Altima, silver, 5ALT321 CA, collision, $500, expired last month).
- "New capture link" form: a policy picker (the four demo policies) or "New policy from your own car" (year, make, model, color, VIN optional, plate, state; creates a trial policy with collision + comprehensive, $500 deductible, active for one year). The claim stores the policy number and a snapshot of the policy at creation.

## 2. Coverage checks (plain code)

- `policy_active`: loss date (from the claimant's answers) inside effective–expiry → green; outside → red "The loss date {date} is outside the policy period ({from} – {to})."; no loss date yet → unchecked.
- `coverage_applies`: incident type vs coverages. Collision-type incidents (crash with another vehicle, hit and run, single-car accident) need collision; hit an animal, vandalism, glass only, hail/flood/tree, parts stolen need comprehensive. Liability-only policy → red "This policy carries liability only; {incident} damage to the insured car is not covered."; covered → green with the coverage name and deductible.
- `vin_match`: VIN read from the VIN photo vs the policy VIN (normalize, ignore spaces) → green/red; policy without VIN → unchecked. Keep `vin_format`.
- Report Review card: a first group "Coverage and vehicle" containing policy_active, coverage_applies, plate_match, vehicle_match, vin_format, vin_match, with a one-paragraph code-built line above the items like "Policy DEMO-AUTO-1001 on file. Collision coverage, $500 deductible, no lienholder. The VIN photo matches the policy. The plate photo matches the policy." (only facts from checks that ran).
- Loss Notice "2. Your vehicle" also shows policy number, year, coverage and deductible.

## 3. Capture details

- Add an optional fifth photo slot `odometer` ("Odometer (optional): switch on the ignition and photograph the odometer reading") with reading `{miles: int|null, readable: bool}`; shown on the Photos step after the four required cards with an "Optional" tag; shown on the Loss Notice and in the package if taken.
- Every required photo card gets a small "Skip with a reason" link: opens an inline textarea (min 10 chars); a skipped slot counts as done for Continue, is shown as "Skipped: {reason}" on the Check step and in the Loss Notice, and produces an amber finding `photo_skipped` "The claimant skipped {slot}: {reason}" (checks that needed that photo report unchecked).
- When a reading comes back `unchecked`, the card shows "We couldn't read this. Retake it?" with the retake button highlighted (keep allowing Continue).

## Tests

Policies seeded; claim creation with a demo policy and with a trial policy; each coverage check's paths (active/expired/missing date; covered/liability-only; VIN match/mismatch/no VIN); the Review card shows "Coverage and vehicle" first; skip-with-reason end to end produces `photo_skipped` and still seals; odometer slot optional (submit works without it, with it the reading is in the package).

Shell access is limited to `uv pip install --python .venv/bin/python -r requirements.txt` and `.venv/bin/python -m pytest -q`. Never run any other command (a blocked command ends your session and loses your work). Do not read files inside .venv. Run pytest at the end and report the exact result.

## 4. Review fixes from step 10 (do these first)

- The claim workspace has buttons and lines that do nothing or show made-up data, copied from the reference screenshot: "Original report", "Reviewed report", "Not delivered: pending (0 attempts)", "Resend", "Resend the report", "Delete media". Remove every one of them. No button may exist without a working action; no line may show data that is not real. Keep "Sealed package" (download), "Verify this package" and "Show the timeline".
- "0 of 0 reviewed" is hard-coded. Show the real count: findings with a Confirm/False alarm label out of all findings that are red or amber (e.g. "1 of 2 reviewed"); hide the line when there are none.
- Seed: each claim's signer name must equal its claimant name, and each seeded claim needs a visible signature image (draw a simple scribble PNG with Pillow).
- Add a test that renders every console page and asserts there is no `<button` without an `onclick`, a `type="submit"` inside a form, or an `href`/form action.
