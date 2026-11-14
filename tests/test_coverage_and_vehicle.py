import base64
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import pytest
from fastapi.testclient import TestClient

from app.db import get_connection, init_db
from app.main import app
from witnessed.checks import (
    check_coverage_applies,
    check_policy_active,
    check_vin_match,
    check_photo_skipped,
    run_checks,
)
from witnessed.providers import FakeProvider
from witnessed.schemas import (
    DamageReading,
    OdometerReading,
    PlateReading,
    Policy,
    VinReading,
    WideReading,
)
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
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGQAAAAyCAYAAACqNX6+AAAAGXRFWHRTb2Z0"
    "d2FyZQBBZG9iZSBJbWFnZVJlYWR5ccllPAAAAyJpVFh0WE1MOmNvbS5hZG9iZS54bXAAAAAAADw/eHBh"
    "Y2tldCBiZWdpbj0i77u/IiBpZD0iVzVNME1wQ2VoaUh6cmVTek5UY3prYzlkIj8+IDx4OnhtcG1ldGEg"
    "eG1sbnM6eD0iYWRvYmU6bnM6bWV0YS8iIHg6eG1wdGs9IkFkb2JlIFhNUCBDb3JlIDUuMC1jMDYwIDYx"
    "LjEzNDI3NywgMjAxMC8xMC8xMi0xNzo0NTowNCAgICAgICAgIj4gPHJkZjpSREYgeG1sbnM6cmRmPSJo"
    "dHRwOi8vd3d3LnczLm9yZy8xOTk5LzAyLzIyLXJkZi1zeW50YXgtbnMjIj4gPHJkZjpEZXNjcmlwdGlv"
    "biByZGY6YWJvdXQ9IiIgeG1sbnM6eG1wPSJodHRwOi8vbnMuYWRvYmUuY29tL3hhcC8xLjAvIj4gPHht"
    "cDpDcmVhdGVEYXRlPjIwMjYtMTAtMjdUMTI6MDA6MDBaPC94bXA6Q3JlYXRlRGF0ZT4gPC9yZGY6RGVz"
    "Y3JpcHRpb24+IDwvcmRmOlJERj4gPC94OnhtcG1ldGE+IDw/eHBhY2tldCBlbmQ9InIiPz6U1B8AAAAH"
    "SURBVHja7J1daBxVFMe/s7sz2d2k2aRNm6bZJk2TNtumaVq0VdqqqNWKVqtVUVEfFEV8EPyA+CD4oPgk"
    "Pvjgk4o+KKKgD4oPPlR8qIhfVbSiVSu0Vds0bbNN0jSZ7M7uzOzcc3ey3U129zPZ3ZnZuzsc5sPM7s2c"
    "3f/93XvOuWfOJJom6KmnnrpX+R9g29p3V91c8Z2q71/4HhW/Rfg21fcqvrfh21z17Vp1v2uV7244D4Pz"
    "UHgPw7mH72Fwfgbn3vA9CN89e7fV90h9j/x3V99P8D3wfwEGAEYV+81QZp1wAAAAAElFTkSuQmCC"
)


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "test_coverage.db"
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


def test_policies_seeded():
    conn = get_connection()
    policies = conn.execute("SELECT * FROM policies ORDER BY policy_number ASC").fetchall()
    conn.close()

    p_map = {p["policy_number"]: p for p in policies}
    assert "DEMO-AUTO-1001" in p_map
    assert "DEMO-AUTO-1002" in p_map
    assert "DEMO-AUTO-1003" in p_map
    assert "DEMO-AUTO-1004" in p_map

    p1 = p_map["DEMO-AUTO-1001"]
    assert p1["vehicle_year"] == 2020
    assert p1["vehicle_make"] == "Toyota"
    assert p1["vehicle_model"] == "Camry"
    assert p1["vehicle_color"].lower() == "blue"
    assert p1["plate"] == "7XYZ123"
    assert p1["state"] == "CA"
    assert p1["vin"] == "1HGCM82633A004352"
    assert p1["collision"] == 1
    assert p1["comprehensive"] == 1
    assert p1["deductible"] == 500

    p3 = p_map["DEMO-AUTO-1003"]
    assert p3["vehicle_make"] == "Ford"
    assert p3["vehicle_model"] == "F-150"
    assert p3["liability_only"] == 1
    assert p3["collision"] == 0

    p4 = p_map["DEMO-AUTO-1004"]
    assert p4["vehicle_make"] == "Nissan"
    assert p4["expiry_date"] < datetime.now(timezone.utc).date().isoformat()


def test_claim_creation_demo_policy(client: TestClient):
    resp = client.post(
        "/console/new",
        data={
            "claimant_name": "Jordan Rivera",
            "policy_picker": "DEMO-AUTO-1001",
            "claimant_statement": "Crash at traffic signal",
        },
    )
    assert resp.status_code == 200
    token = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text).group(1)
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)

    conn = get_connection()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()

    assert claim["policy_number"] == "DEMO-AUTO-1001"
    assert claim["policy_snapshot_json"] is not None
    snap = json.loads(claim["policy_snapshot_json"])
    assert snap["policy_number"] == "DEMO-AUTO-1001"
    assert snap["deductible"] == 500
    assert snap["vehicle_year"] == 2020
    assert snap["collision"] == 1


def test_claim_creation_trial_policy(client: TestClient):
    resp = client.post(
        "/console/new",
        data={
            "claimant_name": "Taylor Swift",
            "policy_picker": "custom",
            "year": "2023",
            "make": "Subaru",
            "model": "Outback",
            "color": "Green",
            "plate": "9SUB123",
            "state": "WA",
            "vin": "4S4BTANC0P3123456",
            "claimant_statement": "Deer ran out",
        },
    )
    assert resp.status_code == 200
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)

    conn = get_connection()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    pol_row = conn.execute("SELECT * FROM policies WHERE policy_number = ?", (claim["policy_number"],)).fetchone()
    conn.close()

    assert claim["policy_number"].startswith("TRIAL-AUTO-")
    assert pol_row is not None
    assert pol_row["collision"] == 1
    assert pol_row["comprehensive"] == 1
    assert pol_row["liability_only"] == 0
    assert pol_row["deductible"] == 500
    assert pol_row["vehicle_make"] == "Subaru"
    assert pol_row["vehicle_year"] == 2023


def test_coverage_checks_paths():
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    last_year = (date.today() - timedelta(days=365)).isoformat()
    two_years_ago = (date.today() - timedelta(days=730)).isoformat()

    # 1. policy_active
    pol_active = Policy(
        effective_date=yesterday,
        expiry_date=tomorrow,
        loss_date=today,
    )
    res = check_policy_active(pol_active)
    assert res.status == "green"

    pol_expired = Policy(
        effective_date=two_years_ago,
        expiry_date=last_year,
        loss_date=today,
    )
    res_exp = check_policy_active(pol_expired)
    assert res_exp.status == "red"
    assert f"The loss date {today} is outside the policy period ({two_years_ago} – {last_year})." in res_exp.reason

    pol_no_date = Policy(
        effective_date=yesterday,
        expiry_date=tomorrow,
        loss_date=None,
    )
    res_no_date = check_policy_active(pol_no_date)
    assert res_no_date.status == "unchecked"

    # 2. coverage_applies
    # Collision incident with collision coverage
    pol_collision = Policy(
        incident_type="Crash with another vehicle",
        collision=True,
        comprehensive=False,
        liability_only=False,
        deductible=500,
    )
    res_c = check_coverage_applies(pol_collision)
    assert res_c.status == "green"
    assert "Collision" in res_c.reason and "$500" in res_c.reason

    # Comprehensive incident with comprehensive coverage
    pol_comp = Policy(
        incident_type="Hit an animal",
        collision=False,
        comprehensive=True,
        liability_only=False,
        deductible=250,
    )
    res_comp = check_coverage_applies(pol_comp)
    assert res_comp.status == "green"
    assert "Comprehensive" in res_comp.reason and "$250" in res_comp.reason

    # Liability only policy
    pol_liab = Policy(
        incident_type="Single-car accident",
        collision=False,
        comprehensive=False,
        liability_only=True,
        deductible=500,
    )
    res_liab = check_coverage_applies(pol_liab)
    assert res_liab.status == "red"
    assert "This policy carries liability only; Single-car accident damage to the insured car is not covered." in res_liab.reason

    # Collision incident on comprehensive-only policy
    pol_mismatch = Policy(
        incident_type="Single-car accident",
        collision=False,
        comprehensive=True,
        liability_only=False,
        deductible=500,
    )
    res_mis = check_coverage_applies(pol_mismatch)
    assert res_mis.status == "red"

    # Missing incident
    pol_no_inc = Policy(
        incident_type=None,
        collision=True,
        comprehensive=True,
    )
    assert check_coverage_applies(pol_no_inc).status == "unchecked"

    # 3. vin_match
    pol_vin = Policy(vin="1HGCM82633A004352")
    read_match = VinReading(vin="1HGCM82633A004352", readable=True)
    assert check_vin_match(pol_vin, read_match).status == "green"

    read_match_spaces = VinReading(vin="1HG CM82 633A 004352", readable=True)
    assert check_vin_match(pol_vin, read_match_spaces).status == "green"

    read_mismatch = VinReading(vin="2T1BURHE7MC123456", readable=True)
    assert check_vin_match(pol_vin, read_mismatch).status == "red"

    pol_no_vin = Policy(vin=None)
    assert check_vin_match(pol_no_vin, read_match).status == "unchecked"


def test_review_card_shows_coverage_and_vehicle_first(client: TestClient):
    resp = client.post(
        "/console/new",
        data={
            "claimant_name": "Robin Buckley",
            "policy_picker": "DEMO-AUTO-1001",
            "claimant_statement": "Side swiped",
        },
    )
    token = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text).group(1)
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)

    client.get(f"/c/{token}")
    ans = {
        "incident_type": "Crash with another vehicle",
        "date": date.today().isoformat(),
        "statement": "Side swiped by passing car",
        "parts": ["Front bumper"],
        "drivable": "Yes",
    }
    client.post(f"/c/{token}/answers", json=ans)

    for slot in ("plate", "vin", "damage", "wide"):
        client.post(f"/c/{token}/photo/{slot}", content=TINY_JPEG, headers={"Content-Type": "image/jpeg"})

    client.post(
        f"/c/{token}/submit",
        json={
            "signer_name": "Robin Buckley",
            "confirmed": True,
            "signature": SAMPLE_SIG,
            "answers": ans,
        },
        headers={"Accept": "application/json"},
    )

    r_report = client.get(f"/console/claims/{claim_id}")
    assert r_report.status_code == 200
    html = r_report.text

    # Review card group 1 must be "1. Coverage and vehicle"
    assert "1. Coverage and vehicle" in html
    assert "Policy DEMO-AUTO-1001 on file" in html
    assert "Collision coverage, $500 deductible, no lienholder." in html

    # Loss notice Section 2 shows policy number, coverage, deductible
    assert "DEMO-AUTO-1001" in html
    assert "$500" in html
    assert "Collision + Comprehensive" in html or "Collision" in html


def test_skip_with_reason_end_to_end(client: TestClient):
    resp = client.post(
        "/console/new",
        data={
            "claimant_name": "Steve Harrington",
            "policy_picker": "DEMO-AUTO-1001",
        },
    )
    token = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text).group(1)
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)

    ans = {
        "incident_type": "Single-car accident",
        "date": date.today().isoformat(),
        "statement": "Hit snow bank",
        "parts": ["Front bumper"],
        "drivable": "Yes",
        "skipped_slots": {
            "vin": "The windshield is covered in ice and the door frame is frozen shut"
        },
    }
    client.post(f"/c/{token}/answers", json=ans)

    # Upload only wide, damage, plate (skip vin)
    for slot in ("wide", "damage", "plate"):
        client.post(f"/c/{token}/photo/{slot}", content=TINY_JPEG, headers={"Content-Type": "image/jpeg"})

    r_sub = client.post(
        f"/c/{token}/submit",
        json={
            "signer_name": "Steve Harrington",
            "confirmed": True,
            "signature": SAMPLE_SIG,
            "answers": ans,
        },
        headers={"Accept": "application/json"},
    )
    assert r_sub.status_code == 200

    conn = get_connection()
    claim = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    findings = conn.execute("SELECT * FROM findings WHERE claim_id = ?", (claim_id,)).fetchall()
    conn.close()

    # Package sealed successfully
    assert claim["package_bytes"] is not None
    assert claim["signature"] is not None
    assert verify(claim["package_bytes"], claim["signature"]) is True

    # Finding photo_skipped is amber
    f_map = {f["check_name"]: f for f in findings}
    assert "photo_skipped" in f_map
    assert f_map["photo_skipped"]["status"] == "amber"
    assert "The claimant skipped vin: The windshield is covered in ice" in f_map["photo_skipped"]["reason"]
    # VIN checks that needed the photo are unchecked
    assert f_map["vin_format"]["status"] == "unchecked"
    assert f_map["vin_match"]["status"] == "unchecked"

    # Report page displays the skipped reason
    r_rep = client.get(f"/console/claims/{claim_id}")
    assert "Skipped: The windshield is covered in ice" in r_rep.text


def test_odometer_slot_optional(client: TestClient, monkeypatch):
    # Case A: submit works without odometer
    resp = client.post("/console/new", data={"claimant_name": "Nancy Wheeler", "policy_picker": "DEMO-AUTO-1002"})
    token = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text).group(1)
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)

    ans = {
        "incident_type": "Vandalism or break-in",
        "date": date.today().isoformat(),
        "statement": "Broken glass",
        "parts": ["Front bumper"],
        "drivable": "Yes",
    }
    client.post(f"/c/{token}/answers", json=ans)
    for slot in ("wide", "damage", "plate", "vin"):
        client.post(f"/c/{token}/photo/{slot}", content=TINY_JPEG, headers={"Content-Type": "image/jpeg"})

    r_sub = client.post(
        f"/c/{token}/submit",
        json={"signer_name": "Nancy Wheeler", "confirmed": True, "signature": SAMPLE_SIG, "answers": ans},
        headers={"Accept": "application/json"},
    )
    assert r_sub.status_code == 200

    conn = get_connection()
    c1 = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    conn.close()
    assert c1["status"] == "ready_for_review"

    # Case B: with odometer, reading is in the package
    resp2 = client.post("/console/new", data={"claimant_name": "Dustin Henderson", "policy_picker": "DEMO-AUTO-1002"})
    token2 = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp2.text).group(1)
    claim_id2 = re.search(r"CLM-[A-F0-9]+", resp2.text).group(0)

    fake_prov = FakeProvider()
    fake_prov.set_reading("odometer", OdometerReading(miles=42150, readable=True))
    monkeypatch.setattr("app.main.get_provider", lambda: fake_prov)

    client.post(f"/c/{token2}/answers", json=ans)
    for slot in ("wide", "damage", "plate", "vin", "odometer"):
        client.post(f"/c/{token2}/photo/{slot}", content=TINY_JPEG, headers={"Content-Type": "image/jpeg"})

    r_sub2 = client.post(
        f"/c/{token2}/submit",
        json={"signer_name": "Dustin Henderson", "confirmed": True, "signature": SAMPLE_SIG, "answers": ans},
        headers={"Accept": "application/json"},
    )
    assert r_sub2.status_code == 200

    conn = get_connection()
    c2 = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id2,)).fetchone()
    conn.close()

    pkg_bytes = c2["package_bytes"]
    pkg_str = pkg_bytes.decode("utf-8")
    assert "odometer" in pkg_str
    assert "42150" in pkg_str

    r_rep2 = client.get(f"/console/claims/{claim_id2}")
    assert "42,150 mi" in r_rep2.text


def test_console_pages_buttons_audit(client: TestClient):
    """Assert there is no <button without an onclick, a type='submit' inside a form, or an href/form action."""
    # First create a claim so we can test detail page and timeline page
    resp = client.post("/console/new", data={"claimant_name": "Audit User", "policy_picker": "DEMO-AUTO-1001"})
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)

    urls_to_test = [
        "/console",
        "/console/new",
        f"/console/claims/{claim_id}",
        "/verify",
    ]

    for url in urls_to_test:
        r = client.get(url)
        assert r.status_code == 200, f"Failed to get {url}"
        html = r.text

        # Find all <button ...> tags
        buttons = re.findall(r"<button\b([^>]*)>", html, re.IGNORECASE)
        for b_attrs in buttons:
            has_onclick = "onclick" in b_attrs.lower()
            is_submit = 'type="submit"' in b_attrs.lower() or "type='submit'" in b_attrs.lower() or "type=" not in b_attrs.lower()
            # If not onclick and not submit, fail
            assert (
                has_onclick or is_submit
            ), f"In {url}: found button without action or submit: <button {b_attrs}>"
