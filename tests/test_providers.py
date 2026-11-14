import os
from unittest.mock import MagicMock, patch
import httpx
import pytest

from witnessed.checks import run_checks
from witnessed.providers import (
    FakeProvider,
    GeminiProvider,
    ReadingProvider,
    get_provider,
)
from witnessed.schemas import (
    DamageReading,
    PlateReading,
    Policy,
    VinReading,
    WideReading,
)


def test_fake_provider_satisfies_protocol():
    provider = FakeProvider()
    assert isinstance(provider, ReadingProvider)


def test_fake_provider_defaults():
    provider = FakeProvider()
    dummy_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"

    wide = provider.read("wide", dummy_bytes)
    assert isinstance(wide, WideReading)
    assert wide.make == "Toyota"
    assert wide.color == "Blue"

    dmg = provider.read("damage", dummy_bytes)
    assert isinstance(dmg, DamageReading)
    assert dmg.damage_visible is True

    plate = provider.read("plate", dummy_bytes)
    assert isinstance(plate, PlateReading)
    assert plate.plate_text == "7XYZ123"
    assert plate.readable is True

    vin = provider.read("vin", dummy_bytes)
    assert isinstance(vin, VinReading)
    assert vin.vin == "1HGCM82633A004352"
    assert vin.readable is True

    assert provider.read("unknown_slot", dummy_bytes) is None


def test_fake_provider_configurable_per_slot():
    provider = FakeProvider()
    custom_plate = PlateReading(plate_text="CUSTOM1", state="NY", readable=True)
    provider.set_reading("plate", custom_plate)

    assert provider.read("plate", b"") == custom_plate
    # Other slots remain default
    assert isinstance(provider.read("wide", b""), WideReading)


def test_fake_provider_slot_set_to_none():
    provider = FakeProvider()
    provider.set_reading("plate", None)
    assert provider.read("plate", b"") is None


def test_get_provider_defaults_to_fake(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    provider = get_provider()
    assert isinstance(provider, FakeProvider)

    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    provider = get_provider()
    assert isinstance(provider, FakeProvider)


def test_get_provider_gemini(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test_key")
    provider = get_provider()
    assert isinstance(provider, GeminiProvider)


def test_provider_returning_none_yields_unchecked_finding():
    provider = FakeProvider({"plate": None})
    policy = Policy(plate="7XYZ123")
    readings = {"plate": provider.read("plate", b"")}

    findings = run_checks(policy, readings)
    plate_finding = next(f for f in findings if f.check == "plate_match")
    assert plate_finding.status == "unchecked"
    assert "missing" in plate_finding.reason.lower()


def test_gemini_provider_success_structured_output():
    provider = GeminiProvider(api_key="mock_key", model="gemini-2.5-flash")
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = '{"plate_text": "7XYZ123", "state": "CA", "readable": true}'
    mock_response.parsed = None
    mock_client.models.generate_content.return_value = mock_response

    with patch.object(provider, "_get_client", return_value=mock_client):
        result = provider.read("plate", b"\xff\xd8\xff\xe0")

    assert isinstance(result, PlateReading)
    assert result.plate_text == "7XYZ123"
    assert result.readable is True
    assert mock_client.models.generate_content.call_count == 1


def test_gemini_provider_invalid_json_returns_none():
    provider = GeminiProvider(api_key="mock_key", model="gemini-2.5-flash")
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = '{"malformed": "json'
    mock_response.parsed = None
    mock_client.models.generate_content.return_value = mock_response

    with patch.object(provider, "_get_client", return_value=mock_client):
        result = provider.read("plate", b"\xff\xd8\xff\xe0")

    assert result is None


def test_gemini_provider_transport_retry_success():
    provider = GeminiProvider(api_key="mock_key", model="gemini-2.5-flash")
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = '{"vin": "1HGCM82633A004352", "readable": true}'
    mock_response.parsed = None

    # First attempt raises transport error, second attempt succeeds
    mock_client.models.generate_content.side_effect = [
        httpx.ConnectError("Network unreachable"),
        mock_response,
    ]

    with patch.object(provider, "_get_client", return_value=mock_client):
        result = provider.read("vin", b"\xff\xd8\xff\xe0")

    assert isinstance(result, VinReading)
    assert result.vin == "1HGCM82633A004352"
    assert mock_client.models.generate_content.call_count == 2


def test_gemini_provider_transport_retry_exhausted_returns_none():
    provider = GeminiProvider(api_key="mock_key", model="gemini-2.5-flash")
    mock_client = MagicMock()

    # Both attempts fail with transport error
    mock_client.models.generate_content.side_effect = [
        httpx.ConnectError("Network unreachable"),
        httpx.ConnectError("Network unreachable again"),
    ]

    with patch.object(provider, "_get_client", return_value=mock_client):
        result = provider.read("vin", b"\xff\xd8\xff\xe0")

    assert result is None
    assert mock_client.models.generate_content.call_count == 2
