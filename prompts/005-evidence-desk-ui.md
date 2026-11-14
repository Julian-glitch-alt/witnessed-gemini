Read AGENTS.md and SPEC.md first. Steps 1–4 are done (73 passing tests); do not break them or change any route, check, seal or provider logic. This step is visual: rebuild the adjuster console as the "Evidence Desk" design shown in two reference screenshots, which are the team's own earlier product design:

- `design-ref/today.png` — the dashboard ("Today").
- `design-ref/claim.png` — the claim workspace.

Open both images and match them as closely as you can with Jinja2 templates + plain CSS + vanilla JS (no frameworks, no build step). Write new code; do not look for or reuse any other codebase.

## Design tokens (put in `app/static/style.css` as CSS variables)

- Fonts (Google Fonts link in `base.html`): "Plus Jakarta Sans" for UI (fallback system-ui, sans-serif), "Source Serif 4" for the claim document, "IBM Plex Mono" for ids and hashes.
- Colors: canvas `#eef1f5` (page background around the app frame), frame `#f7f8fa`, card `#ffffff`, cell `#f3f6f8`, ink `#111c2b`, mute `#5a6b7d`, line `#e4e8ee`, line-strong `#d3dae3`, brand `#1673e6`, brand-deep `#0b4fa8`, good/match `#0f7b6f` on `#e1f4f1`, review/amber `#a66206` on `#fdf1dc`, bad/red `#c4441a` on `#fbe8e1`, notice banner background `#fdf1dc`.
- Shape: the whole app sits in one large rounded frame (radius 28px) with 16px margin on the canvas; cards radius 20px, no heavy shadows (1px line or very soft shadow); pills radius 999px; generous padding (24–28px in cards).

## Layout shell (all console pages: `/console`, `/console/new`, `/console/claims/{id}`, `/verify`)

- Left sidebar (white card, ~240px): logo tile (brand-blue rounded square with a white shield icon, inline SVG) + "Evidence Desk" bold + "Demo Mutual Insurance" small mute. Section label "Menu": "Today" (active state: cell background, blue left bar, count badge = claims needing a decision), "New capture link", "Verify a package". At the bottom: a brand-blue card "Built with Gemini" with one line "Gemini reads every photo. Code checks it. You decide." and an avatar row "AD · Adjuster / Demo desk".
- Top bar (white card): search field (rounded, placeholder "Search claim, claimant, policy or vehicle", filters the claims table client-side), right side: bell icon button, avatar "AD" + "Adjuster" / "Demo desk".
- Below the top bar on every page: a dismissible notice banner: "Shared demo workspace: use only your own car and made-up details. Other visitors can see these claims."
- On ≤900px the sidebar collapses to a top row; on ≤480px everything stacks; nothing overflows horizontally.

## Today page (`/console`) — match `design-ref/today.png`

- Header card: "Today" (large, bold), subline "{weekday}, {month} {day}. {N} need a decision, {M} waiting for the claimant." and a brand-blue pill button "New capture link".
- "Try it in 3 steps" card (dismissible, remembered in localStorage): 1. New capture link → open a claim with your car's make, color and plate. 2. Scan the QR code with your phone and take the four photos live. 3. Come back here about a minute after sending: open the claim, read the review, then decide.
- Four stat cards in a row: "Waiting for your decision" (filled brand-blue card, white text, big number, subline e.g. "2 with a mismatch"), "Mismatch found" (count of claims with any red finding), "Waiting for the claimant" (sessions still open; subline names the latest claimant), "Link to report" (average minutes from claim creation to submission, e.g. "3 min average", subline "From link sent to report ready").
- Claims card (left, ~70% width): title "Claims", filter pills with counts: "Needs me", "Mismatch", "Waiting for claimant", "Decided", "All". Table columns: Claimant (initials avatar circle + name bold + vehicle mute), Claim (mono id + claimed area mute), Suggested lane (pill: "Mismatch" red if any red, "Review" amber if any amber, "Clear" green otherwise, "Waiting" gray if not submitted), Why it is here (one short line from the top finding reason, or "All checks matched." / "No photos yet."), Updated (relative time). Rows link to the claim. Sort: red first, then amber, then clear, then waiting; newest first within a group.
- Right column: "Claims this week" card with seven vertical rounded bars (Mon–Sun or last 7 days) stacked by lane colors and a legend (Clear, Review, Mismatch, Waiting); pure HTML/CSS bars, heights from counts. Below it: "Gemini today" card: photos read, average reading time in seconds, readings that came back unchecked, model used most (from the stored `model` field).

## Claim workspace (`/console/claims/{id}`) — match `design-ref/claim.png`

- Breadcrumb "Today / {claim id} {claimant}, {vehicle}" and on the right a pill "Next claim, k of n" (next claim in the queue order).
- Two columns. Left (~60%): a gray desk panel holding a white "paper" document in the serif font: header row "Loss notice" / "Claim {id}"; centered title "Automobile Loss Notice"; centered line "Submitted by {claimant} through the secure capture link on {date}."; numbered sections with ruled two-column rows (label italic mute, value regular): 1. What happened (statement, claimed area), 2. Your vehicle (make, model, color, plate + state, VIN as read), 3. Photos — a 2×2 grid of the four photos as figures with captions "Figure 1. Whole vehicle, captured live at {time}." etc.; a figure whose slot has a red or amber finding gets a red/amber border and a small numbered marker matching the finding number on the right. Footer: "Session {code} · Seal {first 8 of signature}…, check it on the Verify page" and a round teal "SEALED" stamp (CSS circle with the date) when sealed. Clicking a figure opens the existing photo overlay.
- Right (~40%): "Review" card in the serif font for headings: "Review of claim {id}" / "Ready {date}" small header row; "In short": the AI summary sentences (if present, with small note "Gemini · sentences checked against the findings") or the code summary otherwise. Then the findings grouped as numbered items: each item has a numbered circle (red, amber, gray or green), bold one-line title (the reason), mute line with the check name, and two small buttons "Confirm" / "False alarm" (POST to a new endpoint `/console/claims/{id}/findings/{check}/label` storing the adjuster's label; show the chosen label afterwards). Group headers with a status pill on the right: "Vehicle and plate" (plate_match, vehicle_match, vin_format), "Damage" (damage_present, damage_location), "Photo integrity" (duplicate_photo, photo_reused).
- "Suggested next step" card (amber background when any red/amber, green when clear): small label "Suggested by the checks", big text "Adjuster review" / "Fast track", line "Based on findings 1 to N. You make the decision."
- "Your decision" card: full-width brand-blue "Approve" button, then two-up "Request recapture" and "Refer to SIU" (red text). After a decision, show it with the time. No reject button.
- "Report files" card: rows "Sealed package — anyone can check it on Verify" (download), "Verify this package" (link to /verify).

## New capture link (`/console/new`) and created page

- Same shell; the form in a white card with two-column fields on desktop, grouped "Claimant" and "Policy vehicle". Created page shows the link in a copy field (copy button with "Copied" feedback) and the QR code large, plus "Open the claim" button.

## Verify (`/verify`)

- Same shell; a card with a drop zone style upload and a clear green "Valid signature" or red "Signature does not match: package was altered" result block.

## Tests

Keep all existing tests passing (update text assertions only where wording legitimately changed). Add tests: dashboard shows the four stat cards and filter pills; a red claim sorts above a clear claim; the claim workspace contains "Automobile Loss Notice", "Your decision" and no "reject"; the finding label endpoint accepts only `confirm` / `false_alarm`.

Shell access is limited to `uv pip install --python .venv/bin/python -r requirements.txt` and `.venv/bin/python -m pytest -q`. Never run any other command (a blocked command ends your session and loses your work). Write files with your file tools. Do not read files inside .venv. Run pytest at the end and report the exact result.
