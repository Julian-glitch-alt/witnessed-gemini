import io
import re
from typing import Any
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.db import init_db
from app.main import app
from witnessed.providers import FakeProvider
from witnessed.schemas import DamageReading, PlateReading, VinReading, WideReading
from witnessed.seal import load_or_create_key


def make_jpeg(label: str, color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (320, 240), color=color)
    draw = ImageDraw.Draw(img)
    draw.text((20, 110), label, fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _create_claim_and_token(client: TestClient, form_data: dict[str, str]) -> tuple[str, str]:
    resp = client.post("/console/new", data=form_data)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to create claim: {resp.status_code} {resp.text}")

    match_link = re.search(r"/c/([a-zA-Z0-9_\-]+)", resp.text)
    if not match_link:
        raise RuntimeError("Capture token not found in response")
    token = match_link.group(1)

    match_claim = re.search(r"CLM-[A-F0-9]+", resp.text)
    if not match_claim:
        raise RuntimeError("Claim ID not found in response")
    claim_id = match_claim.group(0)

    return claim_id, token


def _upload_photos_and_submit(
    client: TestClient,
    token: str,
    images: dict[str, bytes],
) -> None:
    for slot in ("wide", "damage", "plate", "vin"):
        resp = client.post(
            f"/c/{token}/photo/{slot}",
            content=images[slot],
            headers={"Content-Type": "image/jpeg"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to upload photo for slot {slot}: {resp.status_code}")

    submit_resp = client.post(
        f"/c/{token}/submit",
        headers={"Accept": "application/json"},
    )
    if submit_resp.status_code != 200:
        raise RuntimeError(f"Failed to submit claim: {submit_resp.status_code}")


def seed() -> list[str]:
    """Seed three demonstration claims with full flow finished."""
    init_db()
    load_or_create_key()

    img_wide = make_jpeg("WIDE VIEW", (30, 60, 140))
    img_damage = make_jpeg("DAMAGE CLOSEUP", (160, 40, 40))
    img_plate = make_jpeg("LICENSE PLATE", (40, 120, 60))
    img_vin = make_jpeg("VIN STICKER", (80, 80, 80))
    images = {
        "wide": img_wide,
        "damage": img_damage,
        "plate": img_plate,
        "vin": img_vin,
    }

    created_claim_ids: list[str] = []

    with TestClient(app) as client:
        # Case (a): Clean case, all green
        import app.main as app_main
        provider_a = FakeProvider()
        app_main.get_provider = lambda: provider_a

        claim_a, token_a = _create_claim_and_token(
            client,
            {
                "make": "Toyota",
                "model": "Camry",
                "color": "Blue",
                "plate": "7XYZ123",
                "state": "CA",
                "claimant_name": "Jane Doe",
                "claimant_statement": "I was rear-ended at a red light; rear bumper crushed.",
                "claimed_damage_area": "rear",
            },
        )
        _upload_photos_and_submit(client, token_a, images)
        created_claim_ids.append(claim_a)

        # Case (b): "Wrong plate": plate reading 8ABC991 vs policy 7XYZ123 -> red
        provider_b = FakeProvider()
        provider_b.set_reading(
            "plate", PlateReading(plate_text="8ABC991", state="CA", readable=True)
        )
        app_main.get_provider = lambda: provider_b

        claim_b, token_b = _create_claim_and_token(
            client,
            {
                "make": "Toyota",
                "model": "Camry",
                "color": "Blue",
                "plate": "7XYZ123",
                "state": "CA",
                "claimant_name": "Bob Miller",
                "claimant_statement": "Rear bumper hit while parked.",
                "claimed_damage_area": "rear",
            },
        )
        _upload_photos_and_submit(client, token_b, images)
        created_claim_ids.append(claim_b)

        # Case (c): "Wrong side": claimed rear, damage read as front -> amber
        provider_c = FakeProvider()
        provider_c.set_reading(
            "wide",
            WideReading(
                make="Toyota",
                model="Camry",
                color="Blue",
                damage_visible=True,
                damage_area="front",
            ),
        )
        provider_c.set_reading(
            "damage",
            DamageReading(
                damage_visible=True,
                damage_type="crush",
                damage_area="front",
                severity="moderate",
            ),
        )
        app_main.get_provider = lambda: provider_c

        claim_c, token_c = _create_claim_and_token(
            client,
            {
                "make": "Toyota",
                "model": "Camry",
                "color": "Blue",
                "plate": "7XYZ123",
                "state": "CA",
                "claimant_name": "Alice Brown",
                "claimant_statement": "Rear-ended in traffic; rear bumper crushed.",
                "claimed_damage_area": "rear",
            },
        )
        _upload_photos_and_submit(client, token_c, images)
        created_claim_ids.append(claim_c)

        # Restore default provider factory
        app_main.get_provider = lambda: FakeProvider()

    print("Demo seed complete. 3 ready claims created:")
    print(f"1. Clean case (All Green): {created_claim_ids[0]} -> /console/claims/{created_claim_ids[0]}")
    print(f"2. Wrong plate (Red): {created_claim_ids[1]} -> /console/claims/{created_claim_ids[1]}")
    print(f"3. Wrong side (Amber): {created_claim_ids[2]} -> /console/claims/{created_claim_ids[2]}")

    return created_claim_ids


if __name__ == "__main__":
    seed()
