import json
from pathlib import Path
import re
from typing import Any
import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app
from witnessed.providers import FakeProvider, ReadingProvider
from witnessed.schemas import (
    DamageReading,
    PlateReading,
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


class StubLiveProvider:
    """Non-fake provider stub implementing the GeminiProvider interface."""

    name: str = "gemini"

    def __init__(
        self,
        readings: dict[str, Any] | None = None,
        raise_slots: set[str] | None = None,
        raise_in_summarize: bool = False,
        model: str = "gemini-2.5-flash",
    ):
        self.name = "gemini"
        self.model = model
        self.last_model = model
        self.readings = dict(readings) if readings is not None else {}
        self.raise_slots = set(raise_slots) if raise_slots else set()
        self.raise_in_summarize = raise_in_summarize
        self.read_calls: list[str] = []
        self.summarize_calls: list[Any] = []

    def read(self, slot: str, image_bytes: bytes) -> Any:
        self.read_calls.append(slot)
        self.last_model = self.model

        if slot in self.raise_slots:
            raise RuntimeError(f"Simulated live provider network/API failure for slot '{slot}'")

        if slot in self.readings:
            return self.readings[slot]

        if slot == "wide":
            return WideReading(
                make="Toyota",
                model="Camry",
                color="Blue",
                damage_visible=True,
                damage_area="rear",
            )
        elif slot == "damage":
            return DamageReading(
                damage_visible=True,
                damage_type="crush",
                damage_area="rear",
                severity="moderate",
            )
        elif slot == "plate":
            return PlateReading(
                plate_text="7XYZ123",
                state="CA",
                readable=True,
            )
        elif slot == "vin":
            return VinReading(
                vin="1HGCM82633A004352",
                readable=True,
            )
        return None

    def summarize(
        self, findings_data: list[dict[str, Any]], retry_prompt: str | None = None
    ) -> dict | None:
        self.summarize_calls.append({"findings": findings_data, "retry_prompt": retry_prompt})
        self.last_model = self.model

        if self.raise_in_summarize:
            raise RuntimeError("Simulated live provider error during summary generation")

        if not findings_data:
            return None

        checks = [f["check"] for f in findings_data]
        green_checks = [f["check"] for f in findings_data if f.get("status") == "green"]
        cites = green_checks[:2] if len(green_checks) >= 2 else (green_checks or [checks[0]])

        return {
            "sentences": [
                {
                    "text": "Automated checks finished evaluating claim records.",
                    "cites": cites,
                }
            ]
        }


@pytest.fixture(autouse=True)
def setup_test_env(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "test_live_paths.db"
    data_dir = tmp_path / "data"
    key_path = tmp_path / "test_seal_key.pem"

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("SEAL_KEY_PATH", str(key_path))
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")

    init_db()
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _execute_full_flow(
    client: TestClient,
    provider: StubLiveProvider,
    monkeypatch,
    expected_statuses: dict[str, str],
):
    assert not isinstance(provider, FakeProvider), "Provider under test must not be FakeProvider"
    assert isinstance(provider, ReadingProvider), "Provider must satisfy ReadingProvider protocol"

    monkeypatch.setattr("app.main.get_provider", lambda: provider)
    monkeypatch.setattr("witnessed.providers.get_provider", lambda: provider)

    # 1. Create claim
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
    create_resp = client.post("/console/new", data=form_data)
    assert create_resp.status_code == 200

    match_link = re.search(r'id="capture-url-input"[^>]*value="[^"]*/c/([a-zA-Z0-9_\-]+)"', create_resp.text)
    if not match_link:
        match_link = re.search(r'href="[^"]*/c/([a-zA-Z0-9_\-]+)"', create_resp.text)
    assert match_link, "Capture link not found in response HTML"
    token = match_link.group(1)

    match_claim = re.search(r"CLM-[A-F0-9]+", create_resp.text)
    assert match_claim, "Claim ID not found in response HTML"
    claim_id = match_claim.group(0)

    # 2. Four uploads
    for slot in ("wide", "damage", "plate", "vin"):
        content = TINY_JPEG + slot.encode()
        upload_resp = client.post(
            f"/c/{token}/photo/{slot}",
            content=content,
            headers={"Content-Type": "image/jpeg"},
        )
        assert upload_resp.status_code == 200, f"Upload for slot '{slot}' must return 200, got {upload_resp.status_code}"
        data = upload_resp.json()
        assert data.get("ok") is True
        assert data.get("slot") == slot
        expected_status = expected_statuses[slot]
        assert data.get("status") == expected_status, (
            f"Expected slot '{slot}' status '{expected_status}', got '{data.get('status')}'"
        )
        assert data.get("model") == provider.model

    # 3. Submit
    ans = {
        "incident_type": "Crash with another vehicle",
        "date": "2026-10-27",
        "statement": "Rear-ended damage.",
        "parts": ["Rear bumper"],
        "drivable": "Yes",
    }
    client.post(f"/c/{token}/answers", json=ans)

    sample_sig = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    submit_resp = client.post(
        f"/c/{token}/submit",
        json={
            "signer_name": "Jane Doe",
            "confirmed": True,
            "signature": sample_sig,
            "answers": ans,
        },
        headers={"Accept": "application/json"},
    )
    assert submit_resp.status_code == 200, f"Submit must return 200, got {submit_resp.status_code}"
    submit_data = submit_resp.json()
    assert submit_data.get("ok") is True

    # 4. Report
    report_resp = client.get(f"/console/claims/{claim_id}")
    assert report_resp.status_code == 200, f"Report page must return 200, got {report_resp.status_code}"
    report_html = report_resp.text
    assert claim_id in report_html
    assert "Checks:" in report_html
    assert "Sealed. Ed25519 signature is valid" in report_html

    # 5. Package download
    pkg_resp = client.get(f"/console/claims/{claim_id}/package")
    assert pkg_resp.status_code == 200
    pkg_wrapper = pkg_resp.json()
    assert "package" in pkg_wrapper
    assert "signature" in pkg_wrapper

    pkg_payload = json.loads(pkg_wrapper["package"])
    assert "readings" in pkg_payload
    for slot in ("wide", "damage", "plate", "vin"):
        slot_info = pkg_payload["readings"][slot]
        assert slot_info["model"] == provider.model

    # 6. Verify valid package
    verify_resp = client.post(
        "/verify",
        files={"package_file": ("package.json", pkg_resp.content, "application/json")},
    )
    assert verify_resp.status_code == 200
    assert "Valid signature" in verify_resp.text

    # 7. Tamper verification
    orig_pkg_str = pkg_wrapper["package"]
    tampered_pkg_str = ("X" if orig_pkg_str[0] != "X" else "Y") + orig_pkg_str[1:]
    tampered_bytes = json.dumps(
        {"package": tampered_pkg_str, "signature": pkg_wrapper["signature"]}
    ).encode("utf-8")
    tampered_resp = client.post(
        "/verify",
        files={"package_file": ("package.json", tampered_bytes, "application/json")},
    )
    assert tampered_resp.status_code == 200
    assert "Signature does not match: package was altered" in tampered_resp.text


def test_live_path_valid_readings(client: TestClient, monkeypatch):
    provider = StubLiveProvider()
    expected = {
        "wide": "read",
        "damage": "read",
        "plate": "read",
        "vin": "read",
    }
    _execute_full_flow(client, provider, monkeypatch, expected)


def test_live_path_none_readings_become_unchecked(client: TestClient, monkeypatch):
    provider = StubLiveProvider(readings={"plate": None, "vin": None})
    expected = {
        "wide": "read",
        "damage": "read",
        "plate": "unchecked",
        "vin": "unchecked",
    }
    _execute_full_flow(client, provider, monkeypatch, expected)


def test_live_path_provider_exception_in_read_becomes_unchecked_no_500(client: TestClient, monkeypatch):
    provider = StubLiveProvider(raise_slots={"wide", "damage"})
    expected = {
        "wide": "unchecked",
        "damage": "unchecked",
        "plate": "read",
        "vin": "read",
    }
    _execute_full_flow(client, provider, monkeypatch, expected)


def test_live_path_all_slots_raise_inside_read(client: TestClient, monkeypatch):
    provider = StubLiveProvider(raise_slots={"wide", "damage", "plate", "vin"})
    expected = {
        "wide": "unchecked",
        "damage": "unchecked",
        "plate": "unchecked",
        "vin": "unchecked",
    }
    _execute_full_flow(client, provider, monkeypatch, expected)


def test_live_path_provider_exception_in_summarize_never_500(client: TestClient, monkeypatch):
    provider = StubLiveProvider(raise_in_summarize=True)
    expected = {
        "wide": "read",
        "damage": "read",
        "plate": "read",
        "vin": "read",
    }
    _execute_full_flow(client, provider, monkeypatch, expected)
