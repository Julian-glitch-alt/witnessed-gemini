import json
from pathlib import Path
import re
import pytest
from fastapi.testclient import TestClient

from app.db import init_db, get_connection
from app.main import app
from witnessed.checks import check_parts_match, run_checks
from witnessed.seal import verify
from witnessed.schemas import (
    DamageReading,
    PlateReading,
    Policy,
    VinReading,
    WideReading,
)

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
    db_file = tmp_path / "test_new_features.db"
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


def _create_claim_and_token(client: TestClient, claimant_name: str = "Jordan Avery") -> tuple[str, str]:
    resp = client.post(
        "/console/new",
        data={
            "make": "Honda",
            "model": "Accord",
            "color": "Gray",
            "plate": "7XYZ123",
            "state": "CA",
            "claimant_name": claimant_name,
            "claimant_statement": "Rear bumper damaged",
            "claimed_damage_area": "rear",
        },
    )
    token = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text).group(1)
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)
    return claim_id, token


# ---------------------------------------------------------------------------
# 1. parts_match tests (overlap, no overlap, empty)
# ---------------------------------------------------------------------------


def test_parts_match_overlap():
    policy = Policy(
        make="Honda",
        model="Accord",
        color="Gray",
        plate="7XYZ123",
        state="CA",
        claimed_parts=["Rear bumper", "Trunk or tailgate"],
    )
    readings = {
        "wide": WideReading(
            make="Honda",
            model="Accord",
            color="Gray",
            damage_visible=True,
            damage_area="rear",
            parts_seen=["Rear bumper"],
        ),
        "damage": DamageReading(
            damage_visible=True,
            damage_type="crush",
            damage_area="rear",
            severity="moderate",
            parts_seen=["Rear bumper"],
        ),
    }
    finding = check_parts_match(policy, readings)
    assert finding.status == "green"
    assert "Rear bumper" in finding.reason


def test_parts_match_no_overlap():
    policy = Policy(
        make="Honda",
        model="Accord",
        color="Gray",
        plate="7XYZ123",
        state="CA",
        claimed_parts=["Front bumper", "Hood"],
    )
    readings = {
        "wide": WideReading(
            make="Honda",
            model="Accord",
            color="Gray",
            damage_visible=True,
            damage_area="rear",
            parts_seen=["Rear bumper"],
        ),
        "damage": DamageReading(
            damage_visible=True,
            damage_type="crush",
            damage_area="rear",
            severity="moderate",
            parts_seen=["Rear bumper"],
        ),
    }
    finding = check_parts_match(policy, readings)
    assert finding.status == "amber"
    assert finding.reason == "The photos show Rear bumper, but the claim lists Front bumper, Hood."


def test_parts_match_empty():
    # Empty on policy side
    policy_empty = Policy(
        make="Honda",
        model="Accord",
        color="Gray",
        plate="7XYZ123",
        state="CA",
        claimed_parts=[],
    )
    readings = {
        "wide": WideReading(
            make="Honda",
            model="Accord",
            color="Gray",
            damage_visible=True,
            damage_area="rear",
            parts_seen=["Rear bumper"],
        ),
        "damage": DamageReading(
            damage_visible=True,
            damage_type="crush",
            damage_area="rear",
            severity="moderate",
            parts_seen=["Rear bumper"],
        ),
    }
    finding = check_parts_match(policy_empty, readings)
    assert finding.status == "unchecked"

    # Empty on readings side
    policy = Policy(
        make="Honda",
        model="Accord",
        color="Gray",
        plate="7XYZ123",
        state="CA",
        claimed_parts=["Rear bumper"],
    )
    readings_empty = {
        "wide": WideReading(
            make="Honda",
            model="Accord",
            color="Gray",
            damage_visible=False,
            damage_area="none",
            parts_seen=[],
        ),
        "damage": DamageReading(
            damage_visible=False,
            damage_type="none",
            damage_area="none",
            severity="minor",
            parts_seen=[],
        ),
    }
    finding2 = check_parts_match(policy, readings_empty)
    assert finding2.status == "unchecked"


# ---------------------------------------------------------------------------
# 2. Recapture round end-to-end
# ---------------------------------------------------------------------------


def test_recapture_round_end_to_end(client: TestClient):
    claim_id, token = _create_claim_and_token(client, "Alex Mercer")

    # Round 1: Open link, answer questions, upload all 4 slots, submit
    client.get(f"/c/{token}")
    ans = {
        "incident_type": "Crash with another vehicle",
        "date": "2026-10-27",
        "statement": "Rear-ended at traffic signal",
        "parts": ["Rear bumper"],
        "drivable": "Yes",
    }
    client.post(f"/c/{token}/answers", json=ans)
    for slot in ("plate", "vin", "damage", "wide"):
        r = client.post(
            f"/c/{token}/photo/{slot}",
            content=TINY_JPEG,
            headers={"Content-Type": "image/jpeg"},
        )
        assert r.status_code == 200

    r_sub = client.post(
        f"/c/{token}/submit",
        json={
            "signer_name": "Alex Mercer",
            "confirmed": True,
            "signature": SAMPLE_SIG,
            "answers": ans,
        },
        headers={"Accept": "application/json"},
    )
    assert r_sub.status_code == 200

    conn = get_connection()
    c1 = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()
    assert c1["status"] == "ready_for_review"
    assert c1["version"] == 1 or c1["version"] is None
    v1_sig = c1["signature"]
    assert v1_sig is not None

    # Adjuster requests recapture of 2 slots: plate and vin
    r_rec = client.post(
        f"/console/claims/{claim_id}/recapture",
        data={"slots": ["plate", "vin"], "reason": "Please retake plate and vin in clearer light"},
    )
    assert r_rec.status_code in (200, 303)

    conn = get_connection()
    c2 = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    s2 = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    conn.close()
    assert c2["status"] == "recapture_requested"
    assert s2["status"] == "open"
    assert json.loads(s2["requested_slots"]) == ["plate", "vin"]

    # In Round 2: uploading to unrequested slots (wide, damage) is rejected with 400
    r_rej1 = client.post(
        f"/c/{token}/photo/wide",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r_rej1.status_code == 400

    r_rej2 = client.post(
        f"/c/{token}/photo/damage",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r_rej2.status_code == 400

    # Uploading to requested slots (plate, vin) succeeds
    r_ok1 = client.post(
        f"/c/{token}/photo/plate",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r_ok1.status_code == 200

    r_ok2 = client.post(
        f"/c/{token}/photo/vin",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    assert r_ok2.status_code == 200

    # Send round 2
    r_send = client.post(
        f"/c/{token}/recapture/send",
        headers={"Accept": "application/json"},
    )
    assert r_send.status_code == 200

    # Verify claim status back to ready_for_review, version 2, previous_signature included
    conn = get_connection()
    c3 = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()
    assert c3["status"] == "ready_for_review"
    assert c3["version"] == 2
    assert c3["previous_signature"] == v1_sig

    # Verify digital seal on version 2 package
    pkg_obj = json.loads(c3["package_bytes"].decode("utf-8"))
    assert pkg_obj["version"] == 2
    assert pkg_obj["previous_signature"] == v1_sig
    assert verify(c3["package_bytes"], c3["signature"]) is True

    # Console claim page displays "Round 2"
    r_rep = client.get(f"/console/claims/{claim_id}")
    assert r_rep.status_code == 200
    assert "Round 2" in r_rep.text

    # Sealed package download serves version 2 package
    r_pkg = client.get(f"/console/claims/{claim_id}/package")
    assert r_pkg.status_code == 200
    dl_data = r_pkg.json()
    assert dl_data["signature"] == c3["signature"]
    dl_pkg = json.loads(dl_data["package"])
    assert dl_pkg["version"] == 2
    assert dl_pkg["previous_signature"] == v1_sig


# ---------------------------------------------------------------------------
# 3. Decisions tests (field inspection, edit-and-approve, no reject)
# ---------------------------------------------------------------------------


def test_decision_field_inspection(client: TestClient):
    claim_id, token = _create_claim_and_token(client, "Taylor Reed")
    r_dec = client.post(
        f"/console/claims/{claim_id}/decision",
        data={"action": "field_inspection", "note": "Dispatch field adjuster for bumper inspection"},
    )
    assert r_dec.status_code in (200, 303)

    conn = get_connection()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    dec = conn.execute(
        "SELECT * FROM decisions WHERE claim_id = ? ORDER BY id DESC", (claim_id,)
    ).fetchone()
    conn.close()

    assert claim["status"] == "field_inspection"
    assert dec["action"] == "field_inspection"
    assert dec["note"] == "Dispatch field adjuster for bumper inspection"


def test_decision_edit_and_approve(client: TestClient):
    claim_id, token = _create_claim_and_token(client, "Morgan Brooks")
    r_dec = client.post(
        f"/console/claims/{claim_id}/decision",
        data={"action": "edit_and_approve", "note": "Approved with adjusted repair hours"},
    )
    assert r_dec.status_code in (200, 303)

    conn = get_connection()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    dec = conn.execute(
        "SELECT * FROM decisions WHERE claim_id = ? ORDER BY id DESC", (claim_id,)
    ).fetchone()
    conn.close()

    assert claim["status"] == "approved"
    assert dec["action"] == "edit_and_approve"
    assert dec["note"] == "Approved with adjusted repair hours"


def test_no_reject_button_in_ui(client: TestClient):
    claim_id, token = _create_claim_and_token(client, "Casey Quinn")
    resp = client.get(f"/console/claims/{claim_id}")
    assert resp.status_code == 200
    html_lower = resp.text.lower()
    # Ensure there is no reject button or auto reject anywhere
    assert "btn-reject" not in html_lower
    assert 'value="reject"' not in html_lower
    assert ">reject<" not in html_lower


# ---------------------------------------------------------------------------
# 4. Notes tests
# ---------------------------------------------------------------------------


def test_claim_notes_save_and_display(client: TestClient):
    claim_id, token = _create_claim_and_token(client, "Sam Rivera")

    note_text = "Claimant verified they parked in garage after accident."
    r_post = client.post(
        f"/console/claims/{claim_id}/notes",
        data={"note": note_text},
    )
    assert r_post.status_code in (200, 303)

    conn = get_connection()
    notes = conn.execute(
        "SELECT * FROM claim_notes WHERE claim_id = ?", (claim_id,)
    ).fetchall()
    conn.close()
    assert len(notes) == 1
    assert notes[0]["note"] == note_text

    # Verify display on report page
    r_view = client.get(f"/console/claims/{claim_id}")
    assert r_view.status_code == 200
    assert note_text in r_view.text


# ---------------------------------------------------------------------------
# 5. Timeline events order
# ---------------------------------------------------------------------------


def test_timeline_events_order(client: TestClient):
    claim_id, token = _create_claim_and_token(client, "Pat Dakota")

    # 1. Link opened
    client.get(f"/c/{token}")

    # 2. Answers saved
    ans = {
        "incident_type": "Crash with another vehicle",
        "date": "2026-10-27",
        "statement": "Rear bumper hit",
        "parts": ["Rear bumper"],
        "drivable": "Yes",
    }
    client.post(f"/c/{token}/answers", json=ans)

    # 3. Photos taken
    for s in ("plate", "vin", "damage", "wide"):
        client.post(
            f"/c/{token}/photo/{s}",
            content=TINY_JPEG,
            headers={"Content-Type": "image/jpeg"},
        )

    # 4. Submitted and sealed
    client.post(
        f"/c/{token}/submit",
        json={
            "signer_name": "Pat Dakota",
            "confirmed": True,
            "signature": SAMPLE_SIG,
            "answers": ans,
        },
        headers={"Accept": "application/json"},
    )

    # 5. Recapture requested
    client.post(
        f"/console/claims/{claim_id}/recapture",
        data={"slots": ["plate"], "reason": "Retake plate"},
    )

    # 6. Round 2 photo taken and sent
    client.post(
        f"/c/{token}/photo/plate",
        content=TINY_JPEG,
        headers={"Content-Type": "image/jpeg"},
    )
    client.post(f"/c/{token}/recapture/send", headers={"Accept": "application/json"})

    # 7. Decision
    client.post(
        f"/console/claims/{claim_id}/decision",
        data={"action": "field_inspection", "note": "Needs onsite inspection"},
    )

    # 8. Note
    client.post(
        f"/console/claims/{claim_id}/notes",
        data={"note": "Assigned to Inspector Bob"},
    )

    # 9. Finding label
    client.post(
        f"/console/claims/{claim_id}/findings/plate_match/label",
        data={"label": "confirm"},
    )

    # Fetch events via API
    r_time = client.get(f"/console/claims/{claim_id}/timeline")
    assert r_time.status_code == 200
    events = r_time.json()["events"]
    event_types = [e["event_type"] for e in events]

    # Verify chronological sequence of event types
    expected_sequence = [
        "claim_created",
        "link_opened",
        "answers_saved",
        "photo_taken",
        "submitted",
        "sealed",
        "recapture_requested",
        "round_2_sent",
        "sealed",
        "decision",
        "note",
        "finding_label",
    ]

    # Check that each expected event type appears in order
    idx = 0
    for expected in expected_sequence:
        found_idx = -1
        for i in range(idx, len(event_types)):
            if event_types[i] == expected:
                found_idx = i
                break
        assert found_idx != -1, f"Expected event '{expected}' not found at or after index {idx} in {event_types}"
        idx = found_idx + 1
