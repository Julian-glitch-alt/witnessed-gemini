import json
from pathlib import Path
import re
import pytest
from fastapi.testclient import TestClient

from app.db import init_db, get_connection
from app.main import app
from witnessed.seal import verify

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

SAMPLE_SIG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "test_flow.db"
    data_dir = tmp_path / "data"
    key_path = tmp_path / "test_flow_seal_key.pem"

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


def _create_claim_and_token(client: TestClient) -> tuple[str, str]:
    resp = client.post(
        "/console/new",
        data={
            "make": "Toyota",
            "model": "Camry",
            "color": "Blue",
            "plate": "7XYZ123",
            "state": "CA",
            "claimant_name": "Jane Claimant",
        },
    )
    assert resp.status_code == 200
    token = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text).group(1)
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)
    return claim_id, token


def test_capture_page_contains_step_titles_and_no_type_file(client: TestClient):
    _, token = _create_claim_and_token(client)
    resp = client.get(f"/c/{token}")
    assert resp.status_code == 200
    html = resp.text

    # Step titles across the 8 steps
    assert "What happened to your car?" in html
    assert "When and where" in html
    assert "What happened" in html
    assert "Where is the damage?" in html
    assert "Photos" in html
    assert "Check" in html
    assert "Sign and send" in html
    # Done step title exists in template or complete page
    assert 'id="step-7"' in html
    assert "Claim sent" in html

    # Absolutely no file input
    html_lower = html.lower()
    assert 'type="file"' not in html_lower
    assert "type='file'" not in html_lower
    assert "type=file" not in html_lower


def test_answers_endpoint_stores_answers_and_maps_parts(client: TestClient):
    claim_id, token = _create_claim_and_token(client)

    # 1. Map ["Rear bumper", "Trunk or tailgate"] -> rear
    ans1 = {
        "incident_type": "Crash with another vehicle",
        "date": "2026-10-25",
        "time": "14:30",
        "place": "Main St intersection",
        "city": "Los Angeles",
        "state": "CA",
        "statement": "I was rear-ended at a red light.",
        "other_vehicle": "Yes",
        "police": "Yes",
        "drivable": "Yes",
        "parts": ["Rear bumper", "Trunk or tailgate"],
        "other_parts": "Slight exhaust scratch",
    }
    r1 = client.post(f"/c/{token}/answers", json=ans1)
    assert r1.status_code == 200
    assert r1.json()["claimed_damage_area"] == "rear"

    conn = get_connection()
    c1 = conn.execute("SELECT claimed_damage_area, claimant_statement, answers_json FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()
    assert c1["claimed_damage_area"] == "rear"
    assert c1["claimant_statement"] == "I was rear-ended at a red light."
    stored_ans1 = json.loads(c1["answers_json"])
    assert stored_ans1["incident_type"] == "Crash with another vehicle"
    assert stored_ans1["parts"] == ["Rear bumper", "Trunk or tailgate"]

    # 2. Map ["Hood", "Rear bumper", "Front left"] -> front (2 front parts vs 1 rear part)
    ans2 = {
        "incident_type": "Crash with another vehicle",
        "date": "2026-10-25",
        "statement": "Front collision with another car.",
        "parts": ["Hood", "Rear bumper", "Front left"],
    }
    r2 = client.post(f"/c/{token}/answers", json=ans2)
    assert r2.status_code == 200
    assert r2.json()["claimed_damage_area"] == "front"

    conn = get_connection()
    c2 = conn.execute("SELECT claimed_damage_area, claimant_statement FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()
    assert c2["claimed_damage_area"] == "front"
    assert c2["claimant_statement"] == "Front collision with another car."


def test_answers_endpoint_rejects_closed_session(client: TestClient):
    _, token = _create_claim_and_token(client)
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(f"/c/{token}/photo/{slot}", content=TINY_JPEG, headers={"Content-Type": "image/jpeg"})

    ans = {
        "incident_type": "Hit and run",
        "date": "2026-10-25",
        "statement": "Hit and run while parked.",
        "parts": ["Left side"],
    }
    client.post(f"/c/{token}/answers", json=ans)
    client.post(
        f"/c/{token}/submit",
        json={
            "signer_name": "Jane Claimant",
            "confirmed": True,
            "signature": SAMPLE_SIG,
            "answers": ans,
        },
        headers={"Accept": "application/json"},
    )

    # After submit, session is closed
    r = client.post(f"/c/{token}/answers", json=ans)
    assert r.status_code == 400


def test_submit_without_signature_is_rejected(client: TestClient):
    _, token = _create_claim_and_token(client)
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(f"/c/{token}/photo/{slot}", content=TINY_JPEG, headers={"Content-Type": "image/jpeg"})

    ans = {
        "incident_type": "Single-car accident",
        "date": "2026-10-25",
        "statement": "Slid on ice into a guardrail.",
        "parts": ["Front bumper"],
    }
    client.post(f"/c/{token}/answers", json=ans)

    # Submit without signature payload
    r1 = client.post(
        f"/c/{token}/submit",
        json={"signer_name": "Jane", "confirmed": True, "signature": ""},
        headers={"Accept": "application/json"},
    )
    assert r1.status_code == 400

    r2 = client.post(
        f"/c/{token}/submit",
        json={"signer_name": "Jane", "confirmed": True},
        headers={"Accept": "application/json"},
    )
    assert r2.status_code == 400


def test_full_flow_produces_sealed_package_with_answers_and_verifies(client: TestClient):
    claim_id, token = _create_claim_and_token(client)

    for slot in ("wide", "damage", "plate", "vin"):
        client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG + slot.encode(),
            headers={"Content-Type": "image/jpeg"},
        )

    ans = {
        "incident_type": "Crash with another vehicle",
        "date": "2026-10-25",
        "time": "15:00",
        "place": "Market St",
        "city": "San Francisco",
        "state": "CA",
        "statement": "Hit at low speed in parking lot.",
        "other_vehicle": "Yes",
        "police": "No",
        "drivable": "Yes",
        "parts": ["Rear bumper"],
        "other_parts": "None",
    }
    r_ans = client.post(f"/c/{token}/answers", json=ans)
    assert r_ans.status_code == 200

    submit_resp = client.post(
        f"/c/{token}/submit",
        json={
            "signer_name": "Jane Claimant",
            "confirmed": True,
            "signature": SAMPLE_SIG,
            "answers": ans,
        },
        headers={"Accept": "application/json"},
    )
    assert submit_resp.status_code == 200

    # Retrieve sealed package
    pkg_resp = client.get(f"/console/claims/{claim_id}/package")
    assert pkg_resp.status_code == 200
    pkg_data = pkg_resp.json()
    canonical_str = pkg_data["package"]
    sig_hex = pkg_data["signature"]

    # Package must verify using witness seal verification
    assert verify(canonical_str.encode("utf-8"), sig_hex) is True

    # Also verify via /verify endpoint
    verify_resp = client.post(
        "/verify",
        files={"package_file": ("package.json", pkg_resp.content, "application/json")},
    )
    assert verify_resp.status_code == 200
    assert "Valid signature" in verify_resp.text

    # Sealed package content checks
    pkg_obj = json.loads(canonical_str)
    assert "answers" in pkg_obj
    assert pkg_obj["answers"]["incident_type"] == "Crash with another vehicle"
    assert pkg_obj["answers"]["statement"] == "Hit at low speed in parking lot."
    assert pkg_obj["answers"]["parts"] == ["Rear bumper"]
    assert pkg_obj["signer_name"] == "Jane Claimant"
    assert pkg_obj["confirmed"] is True
    assert "signature_sha256" in pkg_obj
    assert len(pkg_obj["signature_sha256"]) == 64


def test_claim_workspace_shows_declaration_and_signer_name(client: TestClient):
    claim_id, token = _create_claim_and_token(client)
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(f"/c/{token}/photo/{slot}", content=TINY_JPEG + slot.encode(), headers={"Content-Type": "image/jpeg"})

    ans = {
        "incident_type": "Single-car accident",
        "date": "2026-10-25",
        "statement": "Backed into a low bollard.",
        "parts": ["Rear bumper"],
        "drivable": "Yes",
    }
    client.post(f"/c/{token}/answers", json=ans)
    client.post(
        f"/c/{token}/submit",
        json={
            "signer_name": "Eleanor Vance",
            "confirmed": True,
            "signature": SAMPLE_SIG,
            "answers": ans,
        },
        headers={"Accept": "application/json"},
    )

    report_resp = client.get(f"/console/claims/{claim_id}")
    assert report_resp.status_code == 200
    html = report_resp.text

    assert "5. Declaration" in html
    assert "Eleanor Vance" in html
    assert "I confirm the information in this claim is true to the best of my knowledge." in html
    assert 'class="declaration-signature-img"' in html
