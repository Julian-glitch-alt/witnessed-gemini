import json
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import app
from witnessed.providers import FakeProvider, GeminiProvider
from witnessed.schemas import Finding, SummaryResult
from witnessed.summary import summarize


@pytest.fixture
def sample_findings():
    return [
        Finding(check="plate_match", status="green", reason="Plate 7XYZ123 matches policy."),
        Finding(check="vehicle_match", status="green", reason="Observed vehicle matches policy."),
        Finding(check="damage_present", status="green", reason="Damage photo shows visible damage."),
        Finding(check="duplicate_photo", status="green", reason="All photos are distinct."),
    ]


def test_good_summary_shown(sample_findings):
    good_output = {
        "sentences": [
            {
                "text": "Plate and vehicle specifications match policy records.",
                "cites": ["plate_match", "vehicle_match"],
            },
            {
                "text": "Visible damage confirmed on the vehicle.",
                "cites": ["damage_present"],
            },
        ]
    }
    provider = FakeProvider(canned_summaries=[good_output])
    result = summarize(sample_findings, provider)

    assert result is not None
    assert len(result["sentences"]) == 2
    assert result["sentences"][0]["cites"] == ["plate_match", "vehicle_match"]
    assert len(provider.summary_calls) == 1
    # Check that provider only got findings data, never photos
    call_findings = provider.summary_calls[0]["findings"]
    for f in call_findings:
        assert set(f.keys()) == {"check", "status", "reason"}


def test_bad_summary_rejected_then_retried_success(sample_findings):
    bad_output_forbidden = {
        "sentences": [
            {
                "text": "This genuine claim is verified without fraud.",
                "cites": ["plate_match"],
            }
        ]
    }
    good_output = {
        "sentences": [
            {
                "text": "Vehicle license plate matches policy record.",
                "cites": ["plate_match"],
            }
        ]
    }
    provider = FakeProvider(canned_summaries=[bad_output_forbidden, good_output])
    result = summarize(sample_findings, provider)

    assert result == good_output
    assert len(provider.summary_calls) == 2
    # Verify the retry prompt contains the failure reason
    retry_prompt = provider.summary_calls[1]["retry_prompt"]
    assert retry_prompt is not None
    assert "forbidden word" in retry_prompt.lower()


def test_two_failures_returns_none(sample_findings):
    bad_output_unknown_check = {
        "sentences": [
            {
                "text": "The vehicle was inspected.",
                "cites": ["non_existent_check"],
            }
        ]
    }
    bad_output_no_cites = {
        "sentences": [
            {
                "text": "Inspection completed.",
                "cites": [],
            }
        ]
    }
    provider = FakeProvider(canned_summaries=[bad_output_unknown_check, bad_output_no_cites])
    result = summarize(sample_findings, provider)

    assert result is None
    assert len(provider.summary_calls) == 2


def test_contradiction_rule_rejected(sample_findings):
    # Sentence cites only green checks but contains "mismatch"
    bad_contradiction = {
        "sentences": [
            {
                "text": "A mismatch was observed in the records.",
                "cites": ["plate_match"],  # plate_match is green
            }
        ]
    }
    provider = FakeProvider(canned_summaries=[bad_contradiction, bad_contradiction])
    result = summarize(sample_findings, provider)
    assert result is None
    assert len(provider.summary_calls) == 2
    assert "cites only green checks but contains" in provider.summary_calls[1]["retry_prompt"]


def test_more_than_three_sentences_rejected(sample_findings):
    too_many_sentences = {
        "sentences": [
            {"text": "Sentence 1.", "cites": ["plate_match"]},
            {"text": "Sentence 2.", "cites": ["vehicle_match"]},
            {"text": "Sentence 3.", "cites": ["damage_present"]},
            {"text": "Sentence 4.", "cites": ["duplicate_photo"]},
        ]
    }
    provider = FakeProvider(canned_summaries=[too_many_sentences, too_many_sentences])
    result = summarize(sample_findings, provider)
    assert result is None


def test_report_page_shows_ai_summary_when_valid(monkeypatch):
    good_summary = {
        "sentences": [
            {
                "text": "Vehicle plate and make match policy documents.",
                "cites": ["plate_match", "vehicle_match"],
            }
        ]
    }
    provider = FakeProvider(canned_summaries=[good_summary])
    monkeypatch.setattr("app.main.get_provider", lambda: provider)

    tiny_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"

    with TestClient(app) as client:
        resp = client.post(
            "/console/new",
            data={
                "make": "Toyota",
                "model": "Camry",
                "color": "Blue",
                "plate": "7XYZ123",
                "state": "CA",
                "claimant_name": "Jane",
                "claimant_statement": "Hit rear.",
                "claimed_damage_area": "rear",
            },
        )
        import re
        match_link = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text)
        token = match_link.group(1) if match_link else re.search(r"/c/([a-zA-Z0-9_\-]+)", resp.text).group(1)
        claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)

        for slot in ("wide", "damage", "plate", "vin"):
            client.post(f"/c/{token}/photo/{slot}", content=tiny_jpeg, headers={"Content-Type": "image/jpeg"})
        client.post(f"/c/{token}/submit")

        report_resp = client.get(f"/console/claims/{claim_id}")
        assert report_resp.status_code == 200
        html = report_resp.text

        assert "AI summary (checked against findings)" in html
        assert "Vehicle plate and make match policy documents." in html
        assert "plate_match" in html
        assert "vehicle_match" in html


def test_report_page_omits_ai_summary_when_summarize_fails(monkeypatch):
    bad_summary = {
        "sentences": [
            {
                "text": "This is proven genuine fraud-free evidence.",
                "cites": ["plate_match"],
            }
        ]
    }
    provider = FakeProvider(canned_summaries=[bad_summary, bad_summary])
    monkeypatch.setattr("app.main.get_provider", lambda: provider)

    tiny_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"

    with TestClient(app) as client:
        resp = client.post(
            "/console/new",
            data={
                "make": "Toyota",
                "model": "Camry",
                "color": "Blue",
                "plate": "7XYZ123",
                "state": "CA",
                "claimant_name": "Jane",
                "claimant_statement": "Hit rear.",
                "claimed_damage_area": "rear",
            },
        )
        import re
        match_link = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text)
        token = match_link.group(1) if match_link else re.search(r"/c/([a-zA-Z0-9_\-]+)", resp.text).group(1)
        claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)

        for slot in ("wide", "damage", "plate", "vin"):
            client.post(f"/c/{token}/photo/{slot}", content=tiny_jpeg, headers={"Content-Type": "image/jpeg"})
        client.post(f"/c/{token}/submit")

        report_resp = client.get(f"/console/claims/{claim_id}")
        assert report_resp.status_code == 200
        html = report_resp.text

        # Failed AI summary must not be shown
        assert "AI summary (checked against findings)" not in html
        # Only the code summary is shown
        assert "Checks:" in html


def test_gemini_provider_summarize_mocked():
    provider = GeminiProvider(api_key="fake_key", model="gemini-2.5-flash")
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({
        "sentences": [
            {"text": "Plate matched policy.", "cites": ["plate_match"]}
        ]
    })
    mock_resp.parsed = None
    mock_client.models.generate_content.return_value = mock_resp

    with patch.object(provider, "_get_client", return_value=mock_client):
        result = provider.summarize([{"check": "plate_match", "status": "green", "reason": "ok"}])

    assert result is not None
    assert result["sentences"][0]["text"] == "Plate matched policy."
    assert result["sentences"][0]["cites"] == ["plate_match"]
