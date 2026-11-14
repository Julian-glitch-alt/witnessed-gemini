import json
from pathlib import Path
import re
import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from witnessed.providers import FakeProvider
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


SAMPLE_SIG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _create_and_submit_claim(client: TestClient, plate_text: str = "7XYZ123", prefix: str = "") -> str:
    form_data = {
        "make": "Toyota",
        "model": "Camry",
        "color": "Blue",
        "plate": "7XYZ123",
        "state": "CA",
        "claimant_name": f"{prefix}Jane Doe".strip(),
        "claimant_statement": "Rear bumper damage.",
        "claimed_damage_area": "rear",
    }
    resp = client.post("/console/new", data=form_data)
    assert resp.status_code == 200

    match_link = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text)
    if not match_link:
        match_link = re.search(r"/c/([a-zA-Z0-9_\-]+)", resp.text)
    token = match_link.group(1)
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)

    ans = {
        "incident_type": "Crash with another vehicle",
        "date": "2026-10-27",
        "statement": "Rear bumper damage.",
        "parts": ["Rear bumper"],
        "drivable": "Yes",
    }
    client.post(f"/c/{token}/answers", json=ans)

    for slot in ("wide", "damage", "plate", "vin"):
        content = TINY_JPEG + f"_{prefix}_{slot}".encode()
        client.post(f"/c/{token}/photo/{slot}", content=content, headers={"Content-Type": "image/jpeg"})

    client.post(
        f"/c/{token}/submit",
        json={
            "signer_name": f"{prefix}Jane Doe".strip(),
            "confirmed": True,
            "signature": SAMPLE_SIG,
            "answers": ans,
        },
        headers={"Accept": "application/json"},
    )
    return claim_id


def test_dashboard_shows_four_stat_cards_and_filter_pills(client: TestClient):
    # Add a claim to have data on dashboard
    _create_and_submit_claim(client)

    resp = client.get("/console")
    assert resp.status_code == 200
    html = resp.text

    # 4 Stat cards
    assert "Waiting for your decision" in html
    assert "Mismatch found" in html
    assert "Waiting for the claimant" in html
    assert "Link to report" in html

    # Filter pills
    assert "Needs me" in html
    assert "Mismatch" in html
    assert "Waiting for claimant" in html
    assert "Decided" in html
    assert "All" in html


def test_red_claim_sorts_above_clear_claim(client: TestClient, monkeypatch):
    # Create Claim A (Clear / All Green)
    claim_a_id = _create_and_submit_claim(client, prefix="ClaimA")

    # Configure fake provider to return a wrong plate for Claim B (making it Red Mismatch)
    fake_provider = FakeProvider()
    fake_provider.set_reading(
        "plate",
        PlateReading(plate_text="9ZZZ999", state="CA", readable=True),
    )
    monkeypatch.setattr("app.main.get_provider", lambda: fake_provider)

    # Create Claim B (Red Mismatch)
    claim_b_id = _create_and_submit_claim(client, prefix="ClaimB")

    # In /console, Claim B (red) must sort above Claim A (clear)
    resp = client.get("/console")
    assert resp.status_code == 200
    html = resp.text

    pos_claim_b = html.find(claim_b_id)
    pos_claim_a = html.find(claim_a_id)

    assert pos_claim_b != -1, f"Claim B ({claim_b_id}) not found in console"
    assert pos_claim_a != -1, f"Claim A ({claim_a_id}) not found in console"
    assert pos_claim_b < pos_claim_a, f"Red claim {claim_b_id} should sort above clear claim {claim_a_id}"


def test_claim_workspace_contains_loss_notice_your_decision_and_no_reject(client: TestClient):
    claim_id = _create_and_submit_claim(client)

    resp = client.get(f"/console/claims/{claim_id}")
    assert resp.status_code == 200
    html = resp.text

    # Loss notice document
    assert "Automobile Loss Notice" in html

    # Adjuster decision card
    assert "Your decision" in html

    # Hard rule: No reject button anywhere in code, UI, or text
    assert "reject" not in html.lower()


def test_finding_label_endpoint_accepts_only_confirm_or_false_alarm(client: TestClient):
    claim_id = _create_and_submit_claim(client)

    # 1. Valid: confirm
    resp_confirm = client.post(
        f"/console/claims/{claim_id}/findings/plate_match/label",
        data={"label": "confirm"},
        headers={"Accept": "application/json"},
    )
    assert resp_confirm.status_code == 200
    assert resp_confirm.json()["label"] == "confirm"

    # Verify label persisted in DB
    conn = get_connection()
    row = conn.execute(
        "SELECT adjuster_label FROM findings WHERE claim_id = ? AND check_name = 'plate_match'",
        (claim_id,),
    ).fetchone()
    conn.close()
    assert row["adjuster_label"] == "confirm"

    # 2. Valid: false_alarm
    resp_false_alarm = client.post(
        f"/console/claims/{claim_id}/findings/plate_match/label",
        data={"label": "false_alarm"},
        headers={"Accept": "application/json"},
    )
    assert resp_false_alarm.status_code == 200
    assert resp_false_alarm.json()["label"] == "false_alarm"

    conn = get_connection()
    row = conn.execute(
        "SELECT adjuster_label FROM findings WHERE claim_id = ? AND check_name = 'plate_match'",
        (claim_id,),
    ).fetchone()
    conn.close()
    assert row["adjuster_label"] == "false_alarm"

    # 3. Invalid: reject
    resp_reject = client.post(
        f"/console/claims/{claim_id}/findings/plate_match/label",
        data={"label": "reject"},
    )
    assert resp_reject.status_code == 400

    # 4. Invalid: random string
    resp_invalid = client.post(
        f"/console/claims/{claim_id}/findings/plate_match/label",
        data={"label": "ignore"},
    )
    assert resp_invalid.status_code == 400

    # 5. Invalid: missing label
    resp_empty = client.post(
        f"/console/claims/{claim_id}/findings/plate_match/label",
        data={},
    )
    assert resp_empty.status_code == 400
