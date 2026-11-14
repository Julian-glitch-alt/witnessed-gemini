Read AGENTS.md and SPEC.md first.

Task (step 1 of 5): build the pure core logic of Witnessed as a Python package, with tests. No web server and no UI yet.

Create:
- `requirements.txt` (fastapi, uvicorn, jinja2, python-multipart, google-genai, cryptography, pydantic, pytest, httpx, qrcode).
- Package `witnessed/` with:
  - `schemas.py`: pydantic models for the four slot readings exactly as in SPEC "AI readings", plus `Finding {check, status: red|amber|green|unchecked, reason}`.
  - `vin.py`: `is_valid_vin(vin) -> bool` (17 chars, no I/O/Q, ISO 3779 check digit at position 9).
  - `checks.py`: `run_checks(policy, readings) -> list[Finding]` implementing the five checks in SPEC step 5. A missing or unchecked reading gives `unchecked`, never green. Plate normalization: uppercase, remove spaces and dashes.
  - `providers.py`: a `ReadingProvider` protocol with `read(slot, image_bytes) -> reading | None`; `FakeProvider` returning canned readings (configurable per slot, default = a clean matching case); `GeminiProvider` using `google-genai` with structured JSON output (response schema from the pydantic model), model id from env `GEMINI_MODEL`, key from env `GEMINI_API_KEY`, one retry on transport errors only, returns None on invalid output. `get_provider()` chooses by env `MODEL_PROVIDER` (`fake` default in tests).
  - `seal.py`: canonical JSON (sorted keys, separators without spaces, UTF-8), Ed25519 key load-or-create at env `SEAL_KEY_PATH`, `seal(package) -> (package_bytes, signature_b64)`, `verify(package_bytes, signature_b64, public_key) -> bool`.
- `tests/` covering: valid and invalid VINs (include a known valid VIN such as `1HGCM82633A004352`); each check's red/amber/green/unchecked paths; the two SPEC demo cases (wrong plate → red; front damage with rear claimed → amber); provider returning None → unchecked; seal round trip passes and a one-byte change fails.

Environment: use `.venv/bin/python` (already created). Install deps with `uv pip install --python .venv/bin/python -r requirements.txt`. Run `.venv/bin/python -m pytest -q` at the end and report the exact result. Do not call the real Gemini API.
