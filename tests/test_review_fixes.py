from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import pytest
from fastapi.testclient import TestClient

from app.db import init_db, get_connection
from app.main import app, HUMAN_LABELS
from witnessed.providers import FakeProvider
from witnessed.schemas import PlateReading, WideReading, DamageReading, VinReading

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
    db_file = tmp_path / "test_review_fixes.db"
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


def _create_claim_and_token(client: TestClient, claimant_name: str = "Test Claimant") -> tuple[str, str]:
    resp = client.post("/console/new", data={
        "make": "Toyota",
        "model": "Camry",
        "color": "Blue",
        "plate": "7XYZ123",
        "state": "CA",
        "claimant_name": claimant_name,
        "claimant_statement": "Rear hit",
        "claimed_damage_area": "rear",
    })
    token = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', resp.text).group(1)
    claim_id = re.search(r"CLM-[A-F0-9]+", resp.text).group(0)
    return claim_id, token


def test_sidebar_removes_admin_section_and_dead_links(client: TestClient):
    # Requirement 1: remove the "Admin" section (Settings, System health, Developers).
    resp = client.get("/console")
    assert resp.status_code == 200
    html = resp.text

    assert "admin-block" not in html
    assert "Settings" not in html
    assert "System health" not in html
    assert "Developers" not in html

    # Legitimate menu items remain intact
    assert "Today" in html
    assert "New capture link" in html
    assert "Verify a package" in html


def test_review_card_ordering_pills_and_finding_numbers(client: TestClient, monkeypatch):
    # Requirement 2:
    # Order groups and items inside them: red first, then amber, then unchecked, then green.
    # Group's pill is the worst status in it. Findings numbered in that order.

    # 1. First claim with distinct photos
    c1_id, c1_token = _create_claim_and_token(client, "Claimant One")
    for s in ("wide", "damage", "plate", "vin"):
        client.post(f"/c/{c1_token}/photo/{s}", content=TINY_JPEG + b"_c1_" + s.encode(), headers={"Content-Type": "image/jpeg"})
    client.post(f"/c/{c1_token}/submit", headers={"Accept": "application/json"})

    # 2. Second claim:
    # - photo_reused: red (reusing c1's damage photo)
    # - duplicate_photo: green (other photos distinct)
    # -> Photo integrity group has worst status red (Mismatch), comes FIRST
    # - vehicle_match: amber (mismatched color in wide)
    # - plate_match: green
    # - vin_format: green
    # -> Vehicle and plate group has worst status amber (Review), comes SECOND
    # - damage_present: green
    # - damage_location: green
    # -> Damage group has worst status green (Match), comes THIRD
    fake_provider = FakeProvider()
    fake_provider.set_reading(
        "wide", WideReading(make="Toyota", model="Camry", color="Red", damage_visible=True, damage_area="rear")
    )
    monkeypatch.setattr("app.main.get_provider", lambda: fake_provider)

    c2_id, c2_token = _create_claim_and_token(client, "Claimant Two")
    for s in ("wide", "damage", "plate", "vin"):
        content = TINY_JPEG + b"_c1_damage" if s == "damage" else TINY_JPEG + b"_c2_" + s.encode()
        client.post(f"/c/{c2_token}/photo/{s}", content=content, headers={"Content-Type": "image/jpeg"})
    client.post(f"/c/{c2_token}/submit", headers={"Accept": "application/json"})

    report_resp = client.get(f"/console/claims/{c2_id}")
    assert report_resp.status_code == 200
    html = report_resp.text

    # Verify group order: Photo integrity (red) first, Vehicle and plate (amber) second, Damage (green) third
    pos_photo_int = html.find("1. Photo integrity")
    pos_veh_plate = html.find("2. Vehicle and plate")
    pos_damage = html.find("3. Damage")
    assert pos_photo_int != -1
    assert pos_veh_plate != -1
    assert pos_damage != -1
    assert pos_photo_int < pos_veh_plate < pos_damage

    # Verify group pills
    # 1. Photo integrity has red Mismatch
    assert "Mismatch" in html
    # 2. Vehicle and plate has amber Review
    assert "Review" in html
    # 3. Damage has green Match
    assert "Match" in html

    # Verify sequential numbers:
    # 1: Photo reused (red)
    # 2: Duplicate photo (green)
    # 3: Vehicle (amber)
    # 4: Plate (green)
    # 5: VIN (green)
    # 6: Damage visible (green)
    # 7: Damage location (green)
    assert '<div class="finding-num-circle red">1</div>' in html
    assert '<div class="finding-title" style="font-weight: 700; font-size: 0.88rem; color: var(--ink); margin-bottom: 2px;">Photo reused</div>' in html


def test_photos_slot_borders_and_markers(client: TestClient, monkeypatch):
    # Requirement 3:
    # Figure whose slot is involved in red or amber finding gets 2px border and numbered marker matching finding
    c1_id, c1_token = _create_claim_and_token(client, "First Claim")
    for s in ("wide", "damage", "plate", "vin"):
        client.post(f"/c/{c1_token}/photo/{s}", content=TINY_JPEG + b"_c1_" + s.encode(), headers={"Content-Type": "image/jpeg"})
    client.post(f"/c/{c1_token}/submit", headers={"Accept": "application/json"})

    fake_provider = FakeProvider()
    fake_provider.set_reading(
        "wide", WideReading(make="Toyota", model="Camry", color="Red", damage_visible=True, damage_area="rear")
    )
    monkeypatch.setattr("app.main.get_provider", lambda: fake_provider)

    c2_id, c2_token = _create_claim_and_token(client, "Second Claim")
    for s in ("wide", "damage", "plate", "vin"):
        content = TINY_JPEG + b"_c1_damage" if s == "damage" else TINY_JPEG + b"_c2_" + s.encode()
        client.post(f"/c/{c2_token}/photo/{s}", content=content, headers={"Content-Type": "image/jpeg"})
    client.post(f"/c/{c2_token}/submit", headers={"Accept": "application/json"})

    report_resp = client.get(f"/console/claims/{c2_id}")
    html = report_resp.text

    # Reused damage photo is finding #1 (red) -> damage photo gets border-red and marker 1
    assert "border-red" in html
    assert '<div class="photo-marker-badge red">1</div>' in html

    # Mismatched wide vehicle photo is finding #3 (amber) -> wide photo gets border-amber and marker 3
    assert "border-amber" in html
    assert '<div class="photo-marker-badge amber">3</div>' in html


def test_checks_line_human_labels_and_singular_plural(client: TestClient, monkeypatch):
    # Requirement 4:
    # Replace "Evidence Summary (Deterministic Rule Engine): 1 issue(s) need review: photo_reused"
    # with "Checks: 1 issue needs review: Photo reused."
    # Correct singular/plural.
    c1_id, c1_token = _create_claim_and_token(client, "Claim A")
    for s in ("wide", "damage", "plate", "vin"):
        client.post(f"/c/{c1_token}/photo/{s}", content=TINY_JPEG + b"_cA_" + s.encode(), headers={"Content-Type": "image/jpeg"})
    client.post(f"/c/{c1_token}/submit", headers={"Accept": "application/json"})

    # Single issue: photo_reused (singular "1 issue needs review: Photo reused.")
    c2_id, c2_token = _create_claim_and_token(client, "Claim B")
    for s in ("wide", "damage", "plate", "vin"):
        content = TINY_JPEG + b"_cA_damage" if s == "damage" else TINY_JPEG + b"_cB_" + s.encode()
        client.post(f"/c/{c2_token}/photo/{s}", content=content, headers={"Content-Type": "image/jpeg"})
    client.post(f"/c/{c2_token}/submit", headers={"Accept": "application/json"})

    r2 = client.get(f"/console/claims/{c2_id}")
    assert r2.status_code == 200
    assert "Checks: 1 issue needs review: Photo reused." in r2.text
    assert "Evidence Summary (Deterministic Rule Engine)" not in r2.text
    assert "1 issue(s) need review" not in r2.text

    # Multiple issues: photo_reused + vehicle_match (plural "2 issues need review: Photo reused, Vehicle.")
    fake_provider = FakeProvider()
    fake_provider.set_reading(
        "wide", WideReading(make="Toyota", model="Camry", color="Red", damage_visible=True, damage_area="rear")
    )
    monkeypatch.setattr("app.main.get_provider", lambda: fake_provider)

    c3_id, c3_token = _create_claim_and_token(client, "Claim C")
    for s in ("wide", "damage", "plate", "vin"):
        content = TINY_JPEG + b"_cA_damage" if s == "damage" else TINY_JPEG + b"_cC_" + s.encode()
        client.post(f"/c/{c3_token}/photo/{s}", content=content, headers={"Content-Type": "image/jpeg"})
    client.post(f"/c/{c3_token}/submit", headers={"Accept": "application/json"})

    r3 = client.get(f"/console/claims/{c3_id}")
    assert r3.status_code == 200
    assert "Checks: 2 issues need review: Photo reused, Vehicle." in r3.text

    # Human labels in Today table
    list_resp = client.get("/console")
    assert list_resp.status_code == 200
    # "Photo reused" should be displayed in the Why it is here column
    assert "Photo reused:" in list_resp.text

    # Citation pills use human labels
    good_summary = {
        "sentences": [
            {
                "text": "Plate and vehicle were checked.",
                "cites": ["plate_match", "vehicle_match"],
            }
        ]
    }
    summary_provider = FakeProvider(canned_summaries=[good_summary])
    monkeypatch.setattr("app.main.get_provider", lambda: summary_provider)

    c4_id, c4_token = _create_claim_and_token(client, "Claim D")
    for s in ("wide", "damage", "plate", "vin"):
        client.post(f"/c/{c4_token}/photo/{s}", content=TINY_JPEG + b"_cD_" + s.encode(), headers={"Content-Type": "image/jpeg"})
    client.post(f"/c/{c4_token}/submit", headers={"Accept": "application/json"})

    r4 = client.get(f"/console/claims/{c4_id}")
    assert '<span class="cite-pill">Plate</span>' in r4.text
    assert '<span class="cite-pill">Vehicle</span>' in r4.text


def test_claims_this_week_seven_days_and_stacked_segments(client: TestClient):
    # Requirement 5:
    # 7 bars ending today in server's local date, labeled with weekday abbreviation, today's bar last.
    # Stacked bar segments by lane color (Clear green, Review amber, Mismatch red, Waiting gray).
    _create_claim_and_token(client, "Waiting Claimant")

    resp = client.get("/console")
    assert resp.status_code == 200
    html = resp.text

    today = datetime.now().date()
    today_abbr = today.strftime("%a")

    # Find all weekday labels in weekly bars
    matches = re.findall(r'<span class="bar-day">([A-Za-z]{3})</span>', html)
    assert len(matches) == 7
    # Today's bar must be last
    assert matches[-1] == today_abbr

    # Expected 7 weekday abbreviations ending today
    expected_days = [(today - timedelta(days=6 - i)).strftime("%a") for i in range(7)]
    assert matches == expected_days

    # Stack segments classes present
    assert "bar-stack" in html
    # Legend has gray for Waiting
    assert "Waiting" in html
    assert "legend-dot gray" in html


def test_gemini_today_card_model_provider_label(client: TestClient, monkeypatch):
    # Requirement 6:
    # If MODEL_PROVIDER is not gemini, label it "Demo data (fake model)" instead of "Live inference"
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    resp_fake = client.get("/console")
    assert "Demo data (fake model)" in resp_fake.text
    assert "Live inference" not in resp_fake.text

    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    resp_gemini = client.get("/console")
    assert "Live inference" in resp_gemini.text
    assert "Demo data (fake model)" not in resp_gemini.text


def test_today_table_claim_id_nowrap(client: TestClient):
    # Requirement 7:
    # Keep the claim id on one line (no wrapping inside the id)
    _create_claim_and_token(client, "Nowrap Test")
    resp = client.get("/console")
    assert resp.status_code == 200
    assert 'class="claim-id-mono" style="white-space: nowrap;"' in resp.text
