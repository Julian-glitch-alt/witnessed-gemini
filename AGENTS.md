# Rules for coding agents in this repo

Read `SPEC.md` first. It is the source of truth.

- Do only the task in the prompt. Do not start the next step.
- Never write secrets (API keys, signing keys) into any file in this repo. Read them from environment variables.
- Runtime model calls: Google Gemini only, via `google-genai`. Tests use the fake provider and never call the network.
- Every model call uses structured JSON output; invalid output becomes `unchecked`.
- No automatic rejection anywhere. No "reject" button.
- Keep it small: no frameworks beyond FastAPI + Jinja2 + vanilla JS. No build step.
- No code comments that narrate history or instructions; comment only non-obvious "why".
- When you finish, run `pytest -q` and report the result honestly. Do not claim something works unless you ran it.
- English only in code, UI and docs.
