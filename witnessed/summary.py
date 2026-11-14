import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_finding(f: Any) -> dict[str, str]:
    if hasattr(f, "check") and hasattr(f, "status") and hasattr(f, "reason"):
        return {
            "check": str(f.check),
            "status": str(f.status),
            "reason": str(f.reason),
        }
    if hasattr(f, "keys"):
        d = dict(f)
        check = d.get("check") or d.get("check_name") or ""
        status = d.get("status") or ""
        reason = d.get("reason") or ""
        return {"check": str(check), "status": str(status), "reason": str(reason)}
    check = getattr(f, "check", None) or getattr(f, "check_name", "")
    status = getattr(f, "status", "")
    reason = getattr(f, "reason", "")
    return {"check": str(check), "status": str(status), "reason": str(reason)}


def _verify_summary(result: Any, findings_data: list[dict[str, str]]) -> list[str]:
    failures: list[str] = []
    if not isinstance(result, dict):
        return ["Summary result must be a JSON object."]

    sentences = result.get("sentences")
    if not isinstance(sentences, list) or len(sentences) == 0:
        return ["Summary must contain a list of sentences (1 to 3)."]
    if len(sentences) > 3:
        return ["Summary must contain at most 3 sentences."]

    valid_checks = {f["check"] for f in findings_data}
    green_checks = {f["check"] for f in findings_data if f["status"] == "green"}

    forbidden_words = ["fraud", "fake", "genuine", "verified", "proven", "reject"]
    contradiction_words = ["mismatch", "issue", "concern"]

    for idx, s in enumerate(sentences):
        if not isinstance(s, dict):
            failures.append(f"Sentence {idx + 1} must be an object with text and cites.")
            continue

        text = str(s.get("text", "")).strip()
        cites = s.get("cites")

        if not text:
            failures.append(f"Sentence {idx + 1} text cannot be empty.")

        if not isinstance(cites, list) or len(cites) == 0:
            failures.append(f"Sentence {idx + 1} must cite at least one check name.")
        else:
            for c in cites:
                if c not in valid_checks:
                    failures.append(f"Sentence {idx + 1} cites unknown check '{c}'.")

        text_lower = text.lower()
        for fw in forbidden_words:
            if fw in text_lower:
                failures.append(f"Sentence {idx + 1} contains forbidden word '{fw}'.")

        if isinstance(cites, list) and cites and all(c in green_checks for c in cites):
            for cw in contradiction_words:
                if cw in text_lower:
                    failures.append(
                        f"Sentence {idx + 1} cites only green checks but contains '{cw}'."
                    )

    return failures


def summarize(findings: list[Any], provider: Any) -> dict | None:
    """Produce an objective, verified summary of automated findings."""
    if not findings or not provider:
        return None

    findings_data = [_normalize_finding(f) for f in findings]

    # Attempt 1
    try:
        result = provider.summarize(findings_data)
    except Exception as exc:
        logger.exception("Provider error during summarize attempt 1: %s", exc)
        result = None

    failures = _verify_summary(result, findings_data)
    if not failures:
        return result

    # Attempt 2: retry once with verification errors appended
    retry_prompt = (
        "Previous summary failed verification for the following reasons:\n"
        + "\n".join(f"- {f}" for f in failures)
        + "\nPlease fix these errors and regenerate."
    )
    try:
        result2 = provider.summarize(findings_data, retry_prompt=retry_prompt)
    except Exception as exc:
        logger.exception("Provider error during summarize attempt 2: %s", exc)
        result2 = None
    failures2 = _verify_summary(result2, findings_data)
    if not failures2:
        return result2

    return None
