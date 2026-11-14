import re
from typing import Any

from witnessed.schemas import Finding, Policy, WideReading, DamageReading, PlateReading, VinReading
from witnessed.vin import is_valid_vin


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_plate(val: str | None) -> str:
    if not val:
        return ""
    return re.sub(r"[\s\-]+", "", str(val)).upper()


def _normalize_area(val: str | None) -> str | None:
    if not val:
        return None
    cleaned = str(val).strip().lower().replace("_", " ")
    if "front" in cleaned:
        return "front"
    if "rear" in cleaned or "back" in cleaned:
        return "rear"
    if "left" in cleaned:
        return "left"
    if "right" in cleaned:
        return "right"
    if cleaned in ("none", "unsure"):
        return None
    return cleaned


COLOR_FAMILIES: dict[str, list[str]] = {
    "blue": ["blue", "navy", "azure", "cobalt", "indigo", "teal", "cyan"],
    "gray": ["gray", "grey", "silver", "charcoal", "gunmetal", "slate", "pewter"],
    "white": ["white", "pearl", "ivory", "cream"],
    "black": ["black", "onyx"],
    "red": ["red", "maroon", "burgundy", "crimson", "scarlet", "cherry", "ruby", "wine"],
    "green": ["green", "olive", "emerald", "lime"],
    "yellow": ["yellow", "gold"],
    "brown": ["brown", "tan", "beige", "bronze", "khaki"],
    "orange": ["orange", "copper", "rust"],
    "purple": ["purple", "violet", "magenta"],
}


def _normalize_make(val: Any) -> str:
    if not val:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", str(val)).lower()
    return " ".join(cleaned.split())


def _color_family(val: Any) -> str:
    if not val:
        return ""
    cleaned = re.sub(r"[^\w\s]", " ", str(val)).lower()
    words = cleaned.split()
    if not words:
        return ""
    for family, aliases in COLOR_FAMILIES.items():
        for alias in aliases:
            if alias in words or alias == cleaned:
                return family
    modifiers = {"dark", "light", "metallic", "deep", "bright", "matte", "glossy", "pale", "clear"}
    filtered = [w for w in words if w not in modifiers]
    if filtered:
        return " ".join(filtered)
    return " ".join(words)


def check_duplicate_photo(photos: dict[str, str] | None) -> Finding:
    if not photos:
        return Finding(
            check="duplicate_photo",
            status="green",
            reason="All submitted photos are distinct images.",
        )

    standard_slots = ["wide", "damage", "plate", "vin"]
    ordered_slots = [s for s in standard_slots if s in photos] + [
        s for s in photos if s not in standard_slots
    ]

    seen: dict[str, list[str]] = {}
    for slot in ordered_slots:
        sha = photos.get(slot)
        if sha:
            seen.setdefault(sha, []).append(slot)

    for sha, dup_slots in seen.items():
        if len(dup_slots) >= 2:
            if len(dup_slots) == 2:
                slot_str = f"{dup_slots[0]} and {dup_slots[1]}"
            else:
                slot_str = f"{', '.join(dup_slots[:-1])} and {dup_slots[-1]}"
            return Finding(
                check="duplicate_photo",
                status="amber",
                reason=f"The same image was submitted for slots {slot_str}.",
            )

    return Finding(
        check="duplicate_photo",
        status="green",
        reason="All submitted photos are distinct images.",
    )


def check_photo_reused(
    photos: dict[str, str] | None,
    reused_photos: dict[str, str] | None = None,
) -> Finding:
    if not photos or not reused_photos:
        return Finding(
            check="photo_reused",
            status="green",
            reason="No submitted photos appear in other claims.",
        )

    standard_slots = ["wide", "damage", "plate", "vin"]
    ordered_slots = [s for s in standard_slots if s in photos] + [
        s for s in photos if s not in standard_slots
    ]

    for slot in ordered_slots:
        sha = photos.get(slot)
        if sha and sha in reused_photos:
            other_claim_id = reused_photos[sha]
            return Finding(
                check="photo_reused",
                status="red",
                reason=f"This exact image was already submitted in claim {other_claim_id}.",
            )

    return Finding(
        check="photo_reused",
        status="green",
        reason="No submitted photos appear in other claims.",
    )


def run_checks(
    policy: Any,
    readings: Any,
    photos: dict[str, str] | None = None,
    reused_photos: dict[str, str] | None = None,
) -> list[Finding]:
    """Execute the plain-code checks comparing policy, AI readings, and photos."""
    findings: list[Finding] = []

    # 1. plate_match
    policy_plate = _get_val(policy, "plate")
    plate_reading = _get_val(readings, "plate")
    if not policy_plate or not str(policy_plate).strip():
        findings.append(Finding(
            check="plate_match",
            status="unchecked",
            reason="Policy license plate is not specified.",
        ))
    elif plate_reading is None:
        findings.append(Finding(
            check="plate_match",
            status="unchecked",
            reason="License plate photo reading is missing.",
        ))
    else:
        readable = _get_val(plate_reading, "readable", False)
        reading_plate = _get_val(plate_reading, "plate_text")
        if not readable or not reading_plate or not str(reading_plate).strip():
            findings.append(Finding(
                check="plate_match",
                status="unchecked",
                reason="License plate is unreadable in photo.",
            ))
        else:
            norm_policy = _normalize_plate(policy_plate)
            norm_reading = _normalize_plate(reading_plate)
            if norm_reading == norm_policy:
                findings.append(Finding(
                    check="plate_match",
                    status="green",
                    reason=f"Plate {norm_reading} matches policy plate {norm_policy}.",
                ))
            else:
                findings.append(Finding(
                    check="plate_match",
                    status="red",
                    reason=f"Observed plate {norm_reading} does not match policy plate {norm_policy}.",
                ))

    # 2. vehicle_match
    policy_make = _get_val(policy, "make")
    policy_color = _get_val(policy, "color")
    wide_reading = _get_val(readings, "wide")
    if not policy_make or not policy_color:
        findings.append(Finding(
            check="vehicle_match",
            status="unchecked",
            reason="Policy vehicle make or color is not specified.",
        ))
    elif wide_reading is None:
        findings.append(Finding(
            check="vehicle_match",
            status="unchecked",
            reason="Wide photo reading is missing.",
        ))
    else:
        reading_make = _get_val(wide_reading, "make")
        reading_color = _get_val(wide_reading, "color")

        make_unsure = not reading_make or str(reading_make).strip().lower() in ("", "unsure", "null", "none")
        color_unsure = not reading_color or str(reading_color).strip().lower() in ("", "unsure", "null", "none")

        if make_unsure and color_unsure:
            findings.append(Finding(
                check="vehicle_match",
                status="unchecked",
                reason="Vehicle make and color could not be determined from wide photo.",
            ))
        else:
            norm_policy_make = _normalize_make(policy_make)
            norm_reading_make = _normalize_make(reading_make)
            make_mismatch = (
                not make_unsure
                and norm_reading_make != norm_policy_make
            )

            norm_policy_color = _color_family(policy_color)
            norm_reading_color = _color_family(reading_color)
            color_mismatch = (
                not color_unsure
                and norm_reading_color != norm_policy_color
            )

            if make_mismatch or color_mismatch:
                obs_desc = f"{reading_color or 'unknown color'} {reading_make or 'unknown make'}"
                findings.append(Finding(
                    check="vehicle_match",
                    status="amber",
                    reason=f"Observed vehicle ({obs_desc}) does not match policy ({policy_color} {policy_make}).",
                ))
            elif make_unsure or color_unsure:
                findings.append(Finding(
                    check="vehicle_match",
                    status="unchecked",
                    reason="Vehicle make or color could not be fully confirmed from wide photo.",
                ))
            else:
                findings.append(Finding(
                    check="vehicle_match",
                    status="green",
                    reason=f"Observed vehicle ({reading_color} {reading_make}) matches policy ({policy_color} {policy_make}).",
                ))

    # 3. damage_location
    claimed_area_raw = _get_val(policy, "claimed_damage_area")
    claimed_area = _normalize_area(claimed_area_raw)
    damage_reading = _get_val(readings, "damage")
    if not claimed_area:
        findings.append(Finding(
            check="damage_location",
            status="unchecked",
            reason="Claimed damage area is not specified in policy.",
        ))
    elif damage_reading is None and wide_reading is None:
        findings.append(Finding(
            check="damage_location",
            status="unchecked",
            reason="Damage and wide photo readings are missing.",
        ))
    else:
        dmg_area_raw = _get_val(damage_reading, "damage_area")
        wide_area_raw = _get_val(wide_reading, "damage_area")
        dmg_area = _normalize_area(dmg_area_raw)
        wide_area = _normalize_area(wide_area_raw)

        observed_areas: list[str] = []
        if dmg_area:
            observed_areas.append(dmg_area)
        if wide_area and wide_area not in observed_areas:
            observed_areas.append(wide_area)

        if not observed_areas:
            findings.append(Finding(
                check="damage_location",
                status="unchecked",
                reason="Damage location could not be determined from photos.",
            ))
        else:
            mismatches = [a for a in observed_areas if a != claimed_area]
            matches = [a for a in observed_areas if a == claimed_area]
            if mismatches and not matches:
                obs_str = "/".join(observed_areas)
                findings.append(Finding(
                    check="damage_location",
                    status="amber",
                    reason=f"Damage observed at {obs_str}, but claimed damage area is {claimed_area}.",
                ))
            elif mismatches and matches:
                obs_str = "/".join(observed_areas)
                findings.append(Finding(
                    check="damage_location",
                    status="amber",
                    reason=f"Conflicting damage locations observed ({obs_str}) vs claimed area {claimed_area}.",
                ))
            else:
                findings.append(Finding(
                    check="damage_location",
                    status="green",
                    reason=f"Observed damage location ({claimed_area}) matches claimed damage area.",
                ))

    # 4. damage_present
    if damage_reading is None:
        findings.append(Finding(
            check="damage_present",
            status="unchecked",
            reason="Damage photo reading is missing.",
        ))
    else:
        damage_visible = _get_val(damage_reading, "damage_visible")
        if damage_visible is None:
            findings.append(Finding(
                check="damage_present",
                status="unchecked",
                reason="Damage visibility could not be confirmed from photo.",
            ))
        elif damage_visible is True:
            dtype = _get_val(damage_reading, "damage_type") or "damage"
            findings.append(Finding(
                check="damage_present",
                status="green",
                reason=f"Damage photo shows visible damage ({dtype}).",
            ))
        else:
            findings.append(Finding(
                check="damage_present",
                status="amber",
                reason="Damage photo does not show visible damage.",
            ))

    # 5. vin_format
    vin_reading = _get_val(readings, "vin")
    if vin_reading is None:
        findings.append(Finding(
            check="vin_format",
            status="unchecked",
            reason="VIN photo reading is missing.",
        ))
    else:
        readable = _get_val(vin_reading, "readable", False)
        vin_text = _get_val(vin_reading, "vin")
        if not readable or not vin_text or not str(vin_text).strip():
            findings.append(Finding(
                check="vin_format",
                status="unchecked",
                reason="VIN is unreadable in photo.",
            ))
        else:
            vin_str = str(vin_text).strip().upper()
            if is_valid_vin(vin_str):
                findings.append(Finding(
                    check="vin_format",
                    status="green",
                    reason=f"VIN {vin_str} has 17 characters and passes ISO 3779 check digit.",
                ))
            else:
                findings.append(Finding(
                    check="vin_format",
                    status="amber",
                    reason=f"VIN {vin_str} failed ISO 3779 validation.",
                ))

    # 6. duplicate_photo & 7. photo_reused
    if photos is not None:
        findings.append(check_duplicate_photo(photos))
        findings.append(check_photo_reused(photos, reused_photos))

    return findings
