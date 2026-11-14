# Witnessed (Gemini edition): spec

Verified photo evidence for US auto insurance claims. An adjuster creates a claim and sends the claimant a capture link. The claimant takes four photos with the phone camera inside that link. Gemini reads each photo into structured JSON; plain code compares the readings with the policy and the claimant's statement; the result is sealed with a digital signature and shown to the adjuster, who decides. **The AI reads, the human decides.**

## One flow, end to end

1. **Adjuster creates a claim** (`/console/new`): policy vehicle (make, model, color, plate, state), claimant name, claimant statement (free text, e.g. "I was rear-ended at a light; rear bumper is crushed"), claimed damage area (one of: front, rear, left side, right side). Gets a capture link and a QR code.
2. **Claimant opens the link on a phone** (`/c/{token}`): sees a short session code (e.g. `K7-42`) shown on screen for the whole session, then four capture slots in order: `wide` (whole vehicle), `damage` (close-up of the damage), `plate` (license plate), `vin` (VIN plate / sticker). Each slot uses the live camera via `getUserMedia`; there is **no file picker and no gallery upload**. Each slot allows retake (max 3). After four photos: Submit.
3. **Server receives each photo** only for an open session with a valid token; rejects anything that is not a JPEG/PNG by magic bytes; stores the SHA-256 of the bytes.
4. **Gemini reads each photo** with a slot-specific prompt and returns JSON matching a fixed schema (see "AI readings"). Invalid JSON or schema mismatch = reading status `unchecked`, never a pass. One retry on transport errors only.
5. **Checks (plain code, no LLM)** produce a list of findings, each `red | amber | green | unchecked` with a one-sentence reason that cites the reading:
   - `plate_match`: plate text read from `plate` photo vs policy plate (normalize: uppercase, strip spaces/dashes). Mismatch = red. Unreadable = unchecked.
   - `vehicle_match`: make and color from `wide` photo vs policy. Mismatch = amber.
   - `damage_location`: damage area seen in `wide`/`damage` vs claimed area. Mismatch = amber.
   - `damage_present`: `damage` photo shows visible damage. No damage = amber.
   - `vin_format`: VIN read from `vin` photo has 17 chars and passes the ISO 3779 check digit. Fail = amber. Unreadable = unchecked.
6. **Seal**: when all four photos are read, build a package JSON (claim, photo hashes, readings, findings, timestamps, session code) in canonical form (sorted keys, no whitespace), sign it with Ed25519, store package + signature. The signing key is generated on first start and kept on disk outside the repo (path from env `SEAL_KEY_PATH`).
7. **Adjuster report** (`/console/claims/{id}`): photos, findings with colors and reasons, a short Gemini-written summary that may only restate findings, the seal status. Three buttons: **Approve**, **Request recapture**, **Escalate to SIU**. There is **no reject button** and nothing is ever auto-rejected.
8. **Verify** (`/verify`): upload a sealed package file; page says `Valid signature` or `Signature does not match: package was altered`. Download link for the package on the report page.

## AI readings (Gemini, structured output)

Every call uses Gemini's JSON schema / structured output mode. Claimant text and photos are data, never instructions. Shapes:

- `wide`: `{make, model, color, damage_visible: bool, damage_area: front|rear|left|right|none|unsure}`
- `damage`: `{damage_visible: bool, damage_type: dent|scratch|crush|crack|broken_light|other|none, damage_area, severity: minor|moderate|severe|unsure}`
- `plate`: `{plate_text: string|null, state: string|null, readable: bool}`
- `vin`: `{vin: string|null, readable: bool}`

Any field may be `unsure`/`null`; unsure never counts as a match.

## Hard rules

- Runtime model calls go **only** to Google Gemini via the official `google-genai` SDK. Model id from env `GEMINI_MODEL`, key from env `GEMINI_API_KEY`. Never hardcode either; never commit a key.
- A fake model (`MODEL_PROVIDER=fake`) returns canned readings keyed by photo hash or by slot, so every test runs offline at $0.
- The LLM never decides fraud; it only reads. Findings come from code.
- Stack: Python 3.12, FastAPI, Jinja2 templates + vanilla JS + plain CSS (no build step), SQLite (path from env `DB_PATH`), files on disk (`DATA_DIR`). Deploy target: Modal (single web endpoint, a Modal Volume for `DATA_DIR`).
- Mobile first: the capture page must work at 390px width.
- English everywhere in code, UI and docs.

## Definition of done (all must pass)

1. Create claim → capture link opens at phone width.
2. Capture page has no file input; four slots; retake works.
3. Malformed model output → `unchecked`, never green.
4. Demo case "wrong plate" → `plate_match` red; demo case "damage on front, claimed rear" → `damage_location` amber.
5. Seal verifies; changing one byte of the package → verify fails.
6. No reject button; adjuster actions are Approve / Request recapture / Escalate.
7. `pytest` passes with the fake model; Playwright e2e passes with the fake model; one recorded live run with real Gemini.
8. No secrets in the repo.
9. Public URL on Modal.
