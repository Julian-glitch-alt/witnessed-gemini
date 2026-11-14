import json
import os
from pathlib import Path
import re
import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from witnessed.providers import FakeProvider, get_provider
from witnessed.schemas import PlateReading

TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00"
    b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t"
    b"\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342"
    b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4"
    b"\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "test_witnessed.db"
    data_dir = tmp_path / "data"
    key_path = tmp_path / "test_seal_key.pem"

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("SEAL_KEY_PATH", str(key_path))
    monkeypatch.setenv("MODEL_PROVIDER", "fake")

    init_db()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _create_claim_and_get_token(client: TestClient) -> tuple[str, str]:
    form_data = {
        "make": "Toyota",
        "model": "Camry",
        "color": "Blue",
        "plate": "7XYZ123",
        "state": "CA",
        "claimant_name": "Jane Doe",
        "claimant_statement": "I was rear-ended at a red light.",
        "claimed_damage_area": "rear",
    }
    resp = client.post("/console/new", data=form_data)
    assert resp.status_code == 200

    match_link = re.search(r"/c/([a-zA-Z0-9_\-]+)", resp.text)
    assert match_link, "Capture link not found in response HTML"
    token = match_link.group(1)

    match_claim = re.search(r"CLM-[A-F0-9]+", resp.text)
    assert match_claim, "Claim ID not found in response HTML"
    claim_id = match_claim.group(0)

    return claim_id, token


def test_root_redirects_to_console(client: TestClient):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    assert resp.headers["location"] == "/console"


def test_create_claim_and_capture_link_works(client: TestClient):
    claim_id, token = _create_claim_and_get_token(client)
    resp = client.get(f"/c/{token}")
    assert resp.status_code == 200
    assert "Witnessed Evidence" in resp.text
    assert "Take Photo" in resp.text


def test_capture_page_html_contains_no_type_file(client: TestClient):
    _, token = _create_claim_and_get_token(client)
    resp = client.get(f"/c/{token}")
    assert resp.status_code == 200
    html_lower = resp.text.lower()
    assert 'type="file"' not in html_lower
    assert "type='file'" not in html_lower
    assert "type=file" not in html_lower


def test_upload_non_image_returns_415(client: TestClient):
    _, token = _create_claim_and_get_token(client)
    non_image_data = b"Plain text payload - not a JPEG or PNG"
    resp = client.post(
        f"/c/{token}/photo/wide",
        content=non_image_data,
        headers={"Content-Type": "image/jpeg"},
    )
    assert resp.status_code == 415


def test_upload_to_closed_session_rejected(client: TestClient):
    _, token = _create_claim_and_get_token(client)

    # Upload all 4 required slots
    for slot in ("wide", "damage", "plate", "vin"):
        r = client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG,
            headers={"Content-Type": "image/jpeg"},
        )
        assert r.status_code == 200

    # Submit session
    submit_resp = client.post(
        f"/c/{token}/submit", headers={"Accept": "application/json"}
    )
    assert submit_resp.status_code == 200

    # Attempt another upload to now-closed session
    rejected_resp = client.post(
        f"/c/{token}/photo/wide",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert rejected_resp.status_code in (400, 403)


def test_full_flow_with_four_jpegs_shows_findings(client: TestClient):
    claim_id, token = _create_claim_and_get_token(client)

    for slot in ("wide", "damage", "plate", "vin"):
        r = client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG,
            headers={"Content-Type": "image/jpeg"},
        )
        assert r.status_code == 200

    submit_resp = client.post(
        f"/c/{token}/submit", headers={"Accept": "application/json"}
    )
    assert submit_resp.status_code == 200

    report_resp = client.get(f"/console/claims/{claim_id}")
    assert report_resp.status_code == 200
    text = report_resp.text

    assert "plate_match" in text
    assert "vehicle_match" in text
    assert "damage_location" in text
    assert "damage_present" in text
    assert "vin_format" in text
    assert "Evidence Summary" in text
    assert "Sealed. Ed25519 signature is valid" in text


def test_demo_case_wrong_plate_is_red(client: TestClient, monkeypatch):
    claim_id, token = _create_claim_and_get_token(client)

    # Configure fake provider to return a wrong plate
    fake_provider = FakeProvider()
    fake_provider.set_reading(
        "plate",
        PlateReading(plate_text="9ZZZ999", state="CA", readable=True),
    )
    monkeypatch.setattr("app.main.get_provider", lambda: fake_provider)

    for slot in ("wide", "damage", "plate", "vin"):
        r = client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG,
            headers={"Content-Type": "image/jpeg"},
        )
        assert r.status_code == 200

    submit_resp = client.post(
        f"/c/{token}/submit", headers={"Accept": "application/json"}
    )
    assert submit_resp.status_code == 200

    report_resp = client.get(f"/console/claims/{claim_id}")
    assert report_resp.status_code == 200
    text = report_resp.text

    assert "plate_match" in text
    # Finding row for red mismatch
    assert "findings-row-red" in text
    assert "does not match policy" in text


def test_package_download_and_verify_tamper_detection(client: TestClient):
    claim_id, token = _create_claim_and_get_token(client)

    for slot in ("wide", "damage", "plate", "vin"):
        client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG,
            headers={"Content-Type": "image/jpeg"},
        )

    client.post(f"/c/{token}/submit", headers={"Accept": "application/json"})

    # Download package
    pkg_resp = client.get(f"/console/claims/{claim_id}/package")
    assert pkg_resp.status_code == 200
    pkg_data = pkg_resp.json()
    assert "package" in pkg_data
    assert "signature" in pkg_data

    # Upload exact download to /verify -> Valid signature
    verify_resp = client.post(
        "/verify",
        files={"package_file": ("package.json", pkg_resp.content, "application/json")},
    )
    assert verify_resp.status_code == 200
    assert "Valid signature" in verify_resp.text

    # Flip one character in package canonical string
    original_pkg_str = pkg_data["package"]
    # Change first character
    tampered_pkg_str = (
        ("X" if original_pkg_str[0] != "X" else "Y") + original_pkg_str[1:]
    )
    tampered_bytes = json.dumps(
        {"package": tampered_pkg_str, "signature": pkg_data["signature"]}
    ).encode("utf-8")

    tampered_resp = client.post(
        "/verify",
        files={"package_file": ("package.json", tampered_bytes, "application/json")},
    )
    assert tampered_resp.status_code == 200
    assert "Signature does not match: package was altered" in tampered_resp.text


def test_decision_endpoint_accepts_only_allowed_actions(client: TestClient):
    claim_id, token = _create_claim_and_get_token(client)

    # Invalid actions: reject or unknown
    resp_reject = client.post(
        f"/console/claims/{claim_id}/decision",
        data={"action": "reject"},
    )
    assert resp_reject.status_code == 400

    resp_invalid = client.post(
        f"/console/claims/{claim_id}/decision",
        data={"action": "delete"},
    )
    assert resp_invalid.status_code == 400

    # Valid: approve
    resp_approve = client.post(
        f"/console/claims/{claim_id}/decision",
        data={"action": "approve", "note": "Verified by adjuster"},
        follow_redirects=True,
    )
    assert resp_approve.status_code == 200
    assert "Approved" in resp_approve.text

    # Valid: recapture
    resp_recapture = client.post(
        f"/console/claims/{claim_id}/decision",
        data={"action": "recapture", "note": "Plate was blurry"},
        follow_redirects=True,
    )
    assert resp_recapture.status_code == 200
    assert "Recapture Requested" in resp_recapture.text

    # Valid: escalate
    resp_escalate = client.post(
        f"/console/claims/{claim_id}/decision",
        data={"action": "escalate", "note": "Suspicious vin mismatch"},
        follow_redirects=True,
    )
    assert resp_escalate.status_code == 200
    assert "Escalated SIU" in resp_escalate.text


def test_retake_counter_progression_and_limit(client: TestClient):
    _, token = _create_claim_and_get_token(client)

    # 1st capture: first capture of a slot must not consume a retake (retakes_left is 3)
    r1 = client.post(
        f"/c/{token}/photo/wide",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r1.status_code == 200
    assert r1.json()["retakes_left"] == 3

    # Retake 1: consumes 1 retake (retakes_left is 2)
    r2 = client.post(
        f"/c/{token}/photo/wide",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r2.status_code == 200
    assert r2.json()["retakes_left"] == 2

    # Retake 2: consumes 1 retake (retakes_left is 1)
    r3 = client.post(
        f"/c/{token}/photo/wide",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r3.status_code == 200
    assert r3.json()["retakes_left"] == 1

    # Retake 3: consumes 1 retake (retakes_left is 0)
    r4 = client.post(
        f"/c/{token}/photo/wide",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r4.status_code == 200
    assert r4.json()["retakes_left"] == 0

    # Further upload after 3 retakes returns 409
    r5 = client.post(
        f"/c/{token}/photo/wide",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r5.status_code == 409


def test_honest_wording_report_elements(client: TestClient):
    claim_id, token = _create_claim_and_get_token(client)

    # Distinct images per slot so duplicate_photo is green
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG + slot.encode(),
            headers={"Content-Type": "image/jpeg"},
        )
    client.post(f"/c/{token}/submit", headers={"Accept": "application/json"})

    report_resp = client.get(f"/console/claims/{claim_id}")
    assert report_resp.status_code == 200
    html = report_resp.text

    # Code-built summary line for all green
    assert "No issues found by the automated checks. The adjuster makes the decision." in html
    assert "All checks passed cleanly with verified match." not in html

    # Status pill text: Match instead of Pass
    assert "Match" in html

    # Seal box text and muted line
    assert "Sealed. Ed25519 signature is valid: this package has not changed since the server received it." in html
    assert "The seal shows the package was not altered after receipt. It does not prove what the camera saw." in html

    # Subtitle under evidence photos
    assert "Captured in the live camera flow, session" in html


def test_honest_wording_with_issues_shows_review_count(client: TestClient):
    claim_id, token = _create_claim_and_get_token(client)

    # Identical images for all slots triggers duplicate_photo amber
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG,
            headers={"Content-Type": "image/jpeg"},
        )
    client.post(f"/c/{token}/submit", headers={"Accept": "application/json"})

    report_resp = client.get(f"/console/claims/{claim_id}")
    assert report_resp.status_code == 200
    html = report_resp.text

    assert "1 issue(s) need review: duplicate_photo" in html
    assert "Review" in html


