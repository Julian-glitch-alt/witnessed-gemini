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


def test_what_happened_step_answers_saved_and_shown_in_loss_notice(client: TestClient):
    claim_id, token = _create_claim_and_token(client)
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG + slot.encode(),
            headers={"Content-Type": "image/jpeg"},
        )

    # Claimant answers step "What happened": other_vehicle="Yes", police="Yes", drivable="Yes"
    ans = {
        "incident_type": "Crash with another vehicle",
        "date": "2026-11-14",
        "time": "20:37",
        "place": "Elm St and 5th Ave",
        "city": "San Jose",
        "state": "CA",
        "statement": "Another car backed into me at the intersection.",
        "other_vehicle": "Yes",
        "police": "Yes",
        "drivable": "Yes",
        "parts": ["Rear bumper"],
        "other_parts_note": "None",
    }
    resp_ans = client.post(f"/c/{token}/answers", json=ans)
    assert resp_ans.status_code == 200

    # Verify answers in SQLite
    conn = get_connection()
    c = conn.execute("SELECT answers_json FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()
    stored = json.loads(c["answers_json"])
    assert stored["other_vehicle"] == "Yes"
    assert stored["police"] == "Yes"
    assert stored["drivable"] == "Yes"

    # Submit claim
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

    # View Loss Notice in console
    report_resp = client.get(f"/console/claims/{claim_id}")
    assert report_resp.status_code == 200
    html = report_resp.text

    # Loss Notice must display every chosen answer, never "N/A"
    assert "Was another vehicle involved?" in html
    assert "Were police contacted?" in html
    assert "Can you drive it?" in html

    # Extract table rows
    other_veh_match = re.search(
        r"Was another vehicle involved\?</td>\s*<td class=\"paper-val\">(.*?)</td>",
        html,
    )
    assert other_veh_match, "Row for other vehicle not found"
    assert other_veh_match.group(1).strip() == "Yes"
    assert "N/A" not in other_veh_match.group(1)

    police_match = re.search(
        r"Were police contacted\?</td>\s*<td class=\"paper-val\">(.*?)</td>",
        html,
    )
    assert police_match, "Row for police not found"
    assert police_match.group(1).strip() == "Yes"

    drivable_match = re.search(
        r"Can you drive it\?</td>\s*<td class=\"paper-val\">(.*?)</td>",
        html,
    )
    assert drivable_match, "Row for drivable not found"
    assert drivable_match.group(1).strip() == "Yes"


def test_what_happened_hyphenated_other_vehicle_saved(client: TestClient):
    claim_id, token = _create_claim_and_token(client)
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG + slot.encode(),
            headers={"Content-Type": "image/jpeg"},
        )

    # Post with hyphenated key "other-vehicle" as client DOM might send
    ans = {
        "incident_type": "Crash with another vehicle",
        "date": "2026-11-14",
        "statement": "T-boned at a light.",
        "other-vehicle": "Yes",
        "police": "No",
        "drivable": "No",
        "parts": ["Front bumper"],
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

    report_resp = client.get(f"/console/claims/{claim_id}")
    html = report_resp.text
    other_veh_match = re.search(
        r"Was another vehicle involved\?</td>\s*<td class=\"paper-val\">(.*?)</td>",
        html,
    )
    assert other_veh_match
    assert other_veh_match.group(1).strip() == "Yes"


def test_loss_notice_empty_optional_answers_show_not_given_and_readable_dates(client: TestClient):
    claim_id, token = _create_claim_and_token(client)
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG + slot.encode(),
            headers={"Content-Type": "image/jpeg"},
        )

    # Minimal answers: omit time, place, other_vehicle, police, other_parts_note
    ans = {
        "statement": "Hit a tree branch on a windy day.",
        "parts": ["Hood"],
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

    report_resp = client.get(f"/console/claims/{claim_id}")
    html = report_resp.text

    # Empty optional answers in Loss Notice show "Not given" instead of "N/A"
    assert "Automobile Loss Notice" in html
    loss_notice_start = html.find("Automobile Loss Notice")
    loss_notice_end = html.find("Digital signature") if "Digital signature" in html else len(html)
    loss_notice_html = html[loss_notice_start:loss_notice_end]
    assert "Not given" in loss_notice_html
    assert "N/A" not in loss_notice_html

    # Check Date of the loss & Signed at readable format
    date_loss_match = re.search(
        r"Date of the loss</td>\s*<td class=\"paper-val\">(.*?)</td>",
        html,
    )
    assert date_loss_match
    date_loss_val = date_loss_match.group(1).strip()
    # Readable format like "Nov 14, 2026" or "Nov 14, 2026, 8:37 PM"
    assert re.search(r"[A-Z][a-z]{2} \d{1,2}, \d{4}", date_loss_val), f"Unexpected date of loss format: {date_loss_val}"
    assert "T" not in date_loss_val, f"Raw ISO timestamp found in date of loss: {date_loss_val}"

    signed_at_match = re.search(
        r"Signed at</td>\s*<td class=\"paper-val\">(.*?)</td>",
        html,
    )
    assert signed_at_match
    signed_at_val = signed_at_match.group(1).strip()
    assert re.search(r"[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2} [AP]M", signed_at_val), f"Unexpected signed at format: {signed_at_val}"
    assert "T" not in signed_at_val, f"Raw ISO timestamp found in signed at: {signed_at_val}"


def test_inline_svg_favicon_present_on_every_page(client: TestClient):
    claim_id, token = _create_claim_and_token(client)

    pages_to_check = [
        "/console",
        "/console/new",
        f"/console/claims/{claim_id}",
        "/verify",
        f"/c/{token}",
        f"/c/{token}/complete",
    ]

    expected_favicon_needle = 'link rel="icon" href="data:image/svg+xml'
    for page in pages_to_check:
        resp = client.get(page)
        assert resp.status_code == 200, f"Page {page} returned {resp.status_code}"
        assert expected_favicon_needle in resp.text, f"Favicon missing on {page}"
        # Confirm blue rounded square and white shield
        assert "%232563eb" in resp.text
        assert "%23ffffff" in resp.text

    # /favicon.ico request should not 404
    fav_resp = client.get("/favicon.ico")
    assert fav_resp.status_code in (200, 204)


def test_captured_photo_session_code_watermark_in_bottom_left(client: TestClient):
    _, token = _create_claim_and_token(client)
    resp = client.get(f"/c/{token}")
    assert resp.status_code == 200
    html = resp.text

    # Check that watermark canvas code positions at bottom-left corner
    assert "bottom-left" in html
    # Check dark label background
    assert "rgba(15, 23, 42" in html or "rgba(0, 0, 0" in html
    # Check mono font
    assert "IBM Plex Mono" in html


def test_map_parts_unmapped_parts_returns_none_and_damage_location_unchecked(client: TestClient):
    claim_id, token = _create_claim_and_token(client)
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG + slot.encode(),
            headers={"Content-Type": "image/jpeg"},
        )

    # Claimant selected only "Roof" (does not map to front/rear/left/right)
    ans = {
        "statement": "Tree fell on roof while parked.",
        "parts": ["Roof"],
        "drivable": "Yes",
    }
    ans_resp = client.post(f"/c/{token}/answers", json=ans)
    assert ans_resp.status_code == 200
    assert ans_resp.json()["claimed_damage_area"] is None

    # Check DB claimed_damage_area is None
    conn = get_connection()
    row = conn.execute("SELECT claimed_damage_area FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()
    assert row["claimed_damage_area"] is None

    # Submit claim
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

    # Check damage_location finding is unchecked
    report_resp = client.get(f"/console/claims/{claim_id}")
    assert report_resp.status_code == 200
    html = report_resp.text
    assert "Claimed damage area is not specified" in html

