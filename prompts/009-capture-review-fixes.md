Read AGENTS.md first. Steps 1–8 are done (95 passing tests); do not break them. Review fixes from an end-to-end browser walk of the capture flow:

1. Answer lost: on step "What happened" the claimant tapped "Yes" for "Was another vehicle involved?", but the console Loss Notice shows "N/A". Make sure every answer chosen on that step (other vehicle, police, drivable) is saved to the server and shown in the Loss Notice. Add a test that posts these answers and checks the Loss Notice shows them.
2. `map_parts_to_claimed_area`: when no selected part maps to front/rear/left/right (e.g. only "Roof" or "Wheels or tires"), return `None` (the damage_location check then reports `unchecked`), never a default "rear". Add tests.
3. Loss Notice formatting: show empty optional answers as "Not given" instead of "N/A"; show "Signed at" and "Date of the loss" in a readable form like "Nov 14, 2026, 8:37 PM" (server local time) instead of a raw ISO string.
4. Add an inline SVG favicon (`<link rel="icon" href="data:image/svg+xml,...">`, blue rounded square with a white shield) to every page so `/favicon.ico` is never requested.
5. The session code drawn into each captured photo: keep it fully visible (bottom-left corner, small dark rounded label, white mono text), so the console's finding markers (bottom-right) never cover it.

Shell access is limited to `uv pip install --python .venv/bin/python -r requirements.txt` and `.venv/bin/python -m pytest -q`. Never run any other command (a blocked command ends your session and loses your work). Do not read files inside .venv. Run pytest at the end and report the exact result.
