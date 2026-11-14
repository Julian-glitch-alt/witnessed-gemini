import os
from typing import Any, Protocol, runtime_checkable
import httpx

from witnessed.schemas import (
    DamageReading,
    PlateReading,
    VinReading,
    WideReading,
)

SLOT_SCHEMAS: dict[str, type[Any]] = {
    "wide": WideReading,
    "damage": DamageReading,
    "plate": PlateReading,
    "vin": VinReading,
}

SLOT_PROMPTS: dict[str, str] = {
    "wide": (
        "Identify the vehicle make, model, color, whether vehicle damage is visible, "
        "and the damage area (front, rear, left, right, none, or unsure)."
    ),
    "damage": (
        "Analyze this close-up photo of vehicle damage. Identify whether damage is visible, "
        "the damage type (dent, scratch, crush, crack, broken_light, other, none), "
        "the damage area (front, rear, left, right, none, unsure), and severity (minor, moderate, severe, unsure)."
    ),
    "plate": (
        "Read the vehicle license plate text and state from this photo. "
        "Indicate whether the plate is readable."
    ),
    "vin": (
        "Read the vehicle identification number (VIN) from this photo. "
        "Indicate whether the VIN is readable."
    ),
}


@runtime_checkable
class ReadingProvider(Protocol):
    def read(
        self, slot: str, image_bytes: bytes
    ) -> WideReading | DamageReading | PlateReading | VinReading | None:
        ...


class FakeProvider:
    """Returns canned readings for tests, defaulting to a clean matching vehicle case."""

    def __init__(self, canned_readings: dict[str, Any] | None = None):
        self.canned_readings: dict[str, Any] = dict(canned_readings) if canned_readings else {}

    def set_reading(self, slot: str, reading: Any) -> None:
        self.canned_readings[slot] = reading

    def get_default_reading(self, slot: str) -> Any:
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

    def read(self, slot: str, image_bytes: bytes) -> Any:
        if slot in self.canned_readings:
            return self.canned_readings[slot]
        return self.get_default_reading(slot)


class GeminiProvider:
    """Official Google Gemini provider using google-genai structured JSON output."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise ValueError("GEMINI_API_KEY is not set.")
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def read(self, slot: str, image_bytes: bytes) -> Any:
        schema_cls = SLOT_SCHEMAS.get(slot)
        prompt = SLOT_PROMPTS.get(slot)
        if not schema_cls or not prompt:
            return None

        # Do not attempt network call if API key is missing
        if not self.api_key and not os.environ.get("GEMINI_API_KEY"):
            return None

        mime_type = "image/jpeg"
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            mime_type = "image/png"

        try:
            from google.genai import types
            from google.genai.errors import APIError, ServerError
            transport_errors = (
                ConnectionError,
                TimeoutError,
                httpx.TransportError,
                ServerError,
            )
        except ImportError:
            transport_errors = (
                ConnectionError,
                TimeoutError,
                httpx.TransportError,
            )

        client = self._get_client()
        response = None

        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=self.model,
                    contents=[
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                        prompt,
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=schema_cls,
                    ),
                )
                break
            except transport_errors:
                if attempt == 0:
                    continue
                return None
            except Exception:
                return None

        if response is None:
            return None

        try:
            if getattr(response, "parsed", None) is not None and isinstance(response.parsed, schema_cls):
                return response.parsed
            if getattr(response, "text", None):
                return schema_cls.model_validate_json(response.text)
        except Exception:
            return None

        return None


def get_provider() -> ReadingProvider:
    """Return configured ReadingProvider based on MODEL_PROVIDER env var."""
    provider_name = os.environ.get("MODEL_PROVIDER", "fake").strip().lower()
    if provider_name == "gemini":
        return GeminiProvider()
    return FakeProvider()
