# Witnessed (Gemini Edition)

Witnessed is a verified photo evidence system for US auto insurance claims that pairs Gemini-powered structured extraction with deterministic rule checks and digital cryptographic sealing. The AI reads images while the human adjuster retains full authority over claim decisions with no automatic rejections.

## The One Flow

1. **Adjuster creates claim (`/console/new`)**: The adjuster enters policy vehicle details (make, model, color, plate, state), claimant statement, and claimed damage area. The system generates a dedicated capture link and QR code.
2. **Claimant opens capture link (`/c/{token}`)**: The claimant accesses the link on mobile, observing a fixed session code throughout. Photos must be captured live via camera (`getUserMedia`) across four slots in sequence: `wide`, `damage`, `plate`, and `vin` with real-time visual guide overlays. No gallery upload is permitted.
3. **Server receives and validates evidence**: Uploaded payloads are verified by image magic bytes (JPEG/PNG only) and hashed with SHA-256.
4. **Gemini reads photo data**: Each photo is processed by Google Gemini via `google-genai` structured JSON output matching strict schemas. If an API call fails or times out, it falls back to backup models (e.g., `gemini-3.8-flash` to `gemini-3.5-flash`). Malformed model output becomes `unchecked`.
5. **Deterministic code checks execute**: Plain code compares observations with policy and historical records:
   - `plate_match`: compares plate reading against policy plate (red on mismatch).
   - `vehicle_match`: checks make and color family from wide photo against policy (amber on mismatch).
   - `damage_location`: compares observed damage location against claimed area (amber on mismatch).
   - `damage_present`: checks if damage photo displays visible damage (amber if absent).
   - `vin_format`: validates 17-character VIN format and ISO 3779 check digit (amber on failure).
   - `duplicate_photo`: checks if multiple slots in the same session share identical SHA-256 hashes (amber if duplicated).
   - `photo_reused`: checks if any photo hash was previously submitted under a different claim (red if reused).
6. **Digital seal generated**: The server generates canonical JSON comprising the claim, hashes, readings, findings, and timestamps, and signs it using an Ed25519 private key.
7. **Adjuster report (`/console/claims/{id}`)**: The adjuster reviews the findings ordered by severity, photo thumbnails with interactive full-size inspection overlays, an objective AI summary citing specific checks, and the seal status. The adjuster chooses **Approve**, **Request recapture**, or **Escalate to SIU** (no reject button exists).
8. **Independent verification (`/verify`)**: Sealed package files can be uploaded at any time to verify Ed25519 signature validity against tampering.

## How to Run Locally with Fake Provider

To run offline with zero API cost using deterministic canned readings:

```bash
# Setup virtual environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run demo seeds (creates 4 test claims covering all check states)
export MODEL_PROVIDER=fake
python -m app.seed

# Start web server
uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000/console` in your browser.

## How to Run with Gemini

To run with live Google Gemini model inference:

```bash
export MODEL_PROVIDER=gemini
export GEMINI_API_KEY="your-gemini-api-key"
export GEMINI_MODEL="gemini-3.8-flash,gemini-3.5-flash"

uvicorn app.main:app --reload --port 8000
```

## How to Deploy on Modal

Witnessed is designed to deploy to Modal with persistent volume storage for SQLite and photo files:

1. Create the Modal secret for Gemini:
```bash
modal secret create witnessed-gemini GEMINI_API_KEY="your-gemini-api-key"
```

2. Deploy the application:
```bash
modal deploy modal_app.py
```

## What the Seal Does and Does Not Prove

- **What it proves**: The Ed25519 digital signature over the canonical JSON package proves cryptographic authenticity and tamper-evidence. It guarantees that the claim data, photo SHA-256 hashes, AI readings, deterministic findings, and timestamps have not changed by even a single byte since the server sealed them upon claimant submission.
- **What it does not prove**: The seal does not prove what the camera lens physically witnessed in the real world, whether a photographed scene was staged or altered prior to capture, or whether vehicular damage occurred during the policy period.

## Built With

- **Google Gemini**: Runtime photo reading, structured JSON schema extraction, and findings summarization via the official `google-genai` SDK.
- **Antigravity CLI**: All application code, test suites, and workflows generated from prompt specifications in `prompts/`.
