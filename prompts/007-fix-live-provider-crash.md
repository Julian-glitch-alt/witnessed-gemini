Read AGENTS.md first. Steps 1–6 are done (84 passing tests). A production run with `MODEL_PROVIDER=gemini` crashed. Fix it and close the test gap that hid it.

Production traceback (POST /c/{token}/photo/wide → 500):

```
File "/root/app/main.py", line 561, in upload_photo
    "fake" if isinstance(provider, FakeProvider) else "gemini"
NameError: name 'FakeProvider' is not defined. Did you mean: 'get_provider'?
```

1. Fix the NameError in `app/main.py` (import what is needed, or better: let each provider expose its own name, e.g. a `name` attribute, and record the model id the provider actually used). Search the whole of `app/` and `witnessed/` for any other name that is only defined in tests or only reachable in fake mode, and fix those too.
2. Close the gap: tests currently only exercise `FakeProvider`, so code paths taken with any other provider were never run. Add tests in `tests/test_live_paths.py` that run the full flow (create claim → four uploads → submit → report → package → verify → tamper) through the app with a **non-fake** provider: a small stub class defined in the test that is not `FakeProvider`, with the same interface as `GeminiProvider` (`read` and `summarize`), configured to (a) return valid readings, (b) return `None` for some slots (unchecked), (c) raise inside `read` (must not 500: the reading becomes unchecked). Patch it in the same way the app obtains its provider. Every upload must return 200 with status `read` or `unchecked`, submit must succeed, and the report must render.
3. Also make `upload_photo` and `submit` robust: any exception from the provider becomes an `unchecked` reading for that slot (log the exception), never a 500.

Shell access is limited to `uv pip install --python .venv/bin/python -r requirements.txt` and `.venv/bin/python -m pytest -q`. Never run any other command (a blocked command ends your session and loses your work). Do not read files inside .venv. Run pytest at the end and report the exact result.
