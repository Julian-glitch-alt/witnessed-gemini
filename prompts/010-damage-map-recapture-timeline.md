Read AGENTS.md and SPEC.md first. Steps 1–9 are done; keep all tests passing and do not change the seal format except where stated. Add three features from the team's earlier product design, matching two reference screenshots: `design-ref/claim-damage-diagram.png` (right column "3. Damage" with a car diagram, decision card, notes, report files, more actions) and `design-ref/capture-recapture-round.png` (claimant page for a second capture round). Write new code from the screenshots and this description.

## 1. Damage parts seen in the photos vs listed by the claimant

- Extend the `damage` and `wide` reading schemas with `parts_seen: list[str]` using exactly the claimant's part vocabulary: Front bumper, Hood, Front left, Front right, Left side, Right side, Rear left, Rear right, Rear bumper, Trunk or tailgate, Roof, Windshield, Rear window, Wheels or tires. Update the Gemini prompts to fill it (only parts with visible damage; empty list if none or unsure). FakeProvider: default `["Rear bumper"]`; the seed's "Wrong side" claim returns `["Front bumper","Hood"]`.
- New check `parts_match`: compare parts seen in photos (union of wide + damage) with the parts the claimant listed. Green if they overlap; amber "The photos show {seen}, but the claim lists {listed}." if no overlap; unchecked if either side is empty. Keep `damage_location` as is.
- Claim workspace, Review card, "Damage" group: above its findings show "Seen in the photos: {seen}. Listed by the claimant: {listed}." and a top-down car diagram (inline SVG, same style as the capture step's diagram, ~200px wide, light blue grid background as in the screenshot) where parts seen in photos are filled amber and parts only listed by the claimant are outlined red dashed; labels FRONT / Driver's side (left) / Passenger side (right). Small legend under it.

## 2. Request recapture → second capture round

- On "Request recapture" the adjuster picks one or more slots (checkboxes: License plate, VIN plate, Damage close-up, The whole vehicle) and an optional reason; POST stores a recapture request, reopens the session for only those slots and sets status `recapture_requested`.
- The claimant opening the same link sees the round-2 page from the screenshot: amber card "Your insurer asked for N more photo(s)" + "Take it below, then send. Your answers and other photos stay as you sent them.", progress "0 of N new photos taken", one card per requested slot tagged "Requested again" with the same live camera flow, a "Kept as you sent them" strip of the other photos, "Your answers, as you sent them" read-only cards, and a green "Send the new photo(s)" button.
- On send: replace those slot photos, re-run readings for them and all checks, build and seal a new package version (`version: 2`, includes `previous_signature`), status back to `ready_for_review`. The console shows "Round 2" next to the status and the sealed package download serves the latest version.

## 3. Decision, notes and timeline (right column, as in the screenshot)

- "Your decision" card: Approve (primary), then a 2×2 grid: "Edit and approve", "Request recapture", "Refer to SIU" (red text), "Field inspection". "Edit and approve" opens an inline note field before confirming. Each decision is stored with time and note. Still no reject.
- "Notes" card: textarea "What should the next adjuster know?" + "Save note"; list saved notes with time.
- Timeline: every event appended to a `claim_events` table (claim created, link opened, answers saved, photo taken per slot with model used, submitted, sealed, recapture requested, round 2 sent, decision, note, finding label). "More actions" card with "Show the timeline" toggling a vertical timeline list (time, event, detail).

## Tests

Add tests for parts_match (overlap, no overlap, empty), the recapture round end to end (request two slots → only those accepted → others rejected → send → version 2 package verifies and contains previous_signature), decisions incl. field inspection and edit-and-approve with note, notes, and timeline events order. Keep everything else passing.

Shell access is limited to `uv pip install --python .venv/bin/python -r requirements.txt` and `.venv/bin/python -m pytest -q`. Never run any other command (a blocked command ends your session and loses your work). Do not read files inside .venv. Run pytest at the end and report the exact result.
