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


def map_parts_to_claimed_area(parts: list[str]) -> str | None:
    front_parts = {
        "front bumper",
        "hood",
        "front left",
        "front right",
        "windshield",
    }
    rear_parts = {
        "rear bumper",
        "trunk or tailgate",
        "trunk",
        "tailgate",
        "rear left",
        "rear right",
        "rear window",
    }
    left_parts = {
        "left side",
    }
    right_parts = {
        "right side",
    }

    counts = {"front": 0, "rear": 0, "left": 0, "right": 0}
    for p in parts:
        cleaned = str(p).strip().lower()
        if cleaned in front_parts:
            counts["front"] += 1
        elif cleaned in rear_parts:
            counts["rear"] += 1
        elif cleaned in left_parts:
            counts["left"] += 1
        elif cleaned in right_parts:
            counts["right"] += 1
        else:
            if any(fp in cleaned for fp in ("front", "hood", "windshield")):
                counts["front"] += 1
            elif any(rp in cleaned for rp in ("rear", "trunk", "tailgate")):
                counts["rear"] += 1
            elif "left" in cleaned:
                counts["left"] += 1
            elif "right" in cleaned:
                counts["right"] += 1

    best_area = None
    best_count = 0
    for area in ("front", "rear", "left", "right"):
        if counts[area] > best_count:
            best_count = counts[area]
            best_area = area

    return best_area


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


def check_parts_match(policy: Any, readings: Any) -> Finding:
    wide_reading = _get_val(readings, "wide")
    damage_reading = _get_val(readings, "damage")

    seen_parts: list[str] = []
    for r in (wide_reading, damage_reading):
        if r is not None:
            parts = _get_val(r, "parts_seen", []) or []
            for p in parts:
                p_str = str(p).strip()
                if p_str and p_str not in seen_parts:
                    seen_parts.append(p_str)

    claimed_parts_raw = _get_val(policy, "claimed_parts") or _get_val(policy, "parts") or []
    if isinstance(claimed_parts_raw, str):
        claimed_parts_raw = [claimed_parts_raw]

    listed_parts: list[str] = []
    for p in claimed_parts_raw:
        p_str = str(p).strip()
        if p_str and p_str not in listed_parts:
            listed_parts.append(p_str)

    if not seen_parts or not listed_parts:
        return Finding(
            check="parts_match",
            status="unchecked",
            reason="Claimant listed parts or photo damage parts are not specified.",
        )

    seen_lower = {p.lower() for p in seen_parts}
    listed_lower = {p.lower() for p in listed_parts}
    overlap = seen_lower.intersection(listed_lower)

    seen_str = ", ".join(seen_parts)
    listed_str = ", ".join(listed_parts)

    if overlap:
        return Finding(
            check="parts_match",
            status="green",
            reason=f"Observed damage parts ({seen_str}) match claimant listed parts ({listed_str}).",
        )
    else:
        return Finding(
            check="parts_match",
            status="amber",
            reason=f"The photos show {seen_str}, but the claim lists {listed_str}.",
        )


def check_policy_active(policy: Any) -> Finding:
    loss_date = _get_val(policy, "loss_date")
    eff = _get_val(policy, "effective_date")
    exp = _get_val(policy, "expiry_date")

    if not loss_date or not str(loss_date).strip():
        return Finding(
            check="policy_active",
            status="unchecked",
            reason="Loss date has not been provided yet.",
        )
    if not eff or not exp:
        return Finding(
            check="policy_active",
            status="unchecked",
            reason="Policy effective and expiry dates are not available.",
        )

    l_str = str(loss_date).strip()
    e_str = str(eff).strip()
    x_str = str(exp).strip()

    if e_str <= l_str <= x_str:
        return Finding(
            check="policy_active",
            status="green",
            reason=f"The loss date {l_str} is within the policy period ({e_str} – {x_str}).",
        )
    else:
        return Finding(
            check="policy_active",
            status="red",
            reason=f"The loss date {l_str} is outside the policy period ({e_str} – {x_str}).",
        )


def check_coverage_applies(policy: Any) -> Finding:
    incident_raw = _get_val(policy, "incident_type")
    if not incident_raw or not str(incident_raw).strip():
        return Finding(
            check="coverage_applies",
            status="unchecked",
            reason="Incident type has not been provided yet.",
        )

    incident = str(incident_raw).strip()
    inc_lower = incident.lower()

    is_liability_only = bool(_get_val(policy, "liability_only", False))
    collision = bool(_get_val(policy, "collision", False))
    comprehensive = bool(_get_val(policy, "comprehensive", False))
    deductible = _get_val(policy, "deductible", 500)

    if is_liability_only:
        return Finding(
            check="coverage_applies",
            status="red",
            reason=f"This policy carries liability only; {incident} damage to the insured car is not covered.",
        )

    is_collision_type = (
        "crash" in inc_lower
        or "hit and run" in inc_lower
        or "single-car" in inc_lower
        or "single car" in inc_lower
    )

    is_comprehensive_type = (
        "animal" in inc_lower
        or "vandalism" in inc_lower
        or "break-in" in inc_lower
        or "glass" in inc_lower
        or "hail" in inc_lower
        or "flood" in inc_lower
        or "tree" in inc_lower
        or "stolen" in inc_lower
        or "theft" in inc_lower
        or "fire" in inc_lower
    )

    if is_collision_type:
        if collision:
            return Finding(
                check="coverage_applies",
                status="green",
                reason=f"Covered under Collision coverage (${deductible} deductible).",
            )
        else:
            return Finding(
                check="coverage_applies",
                status="red",
                reason=f"This policy does not carry collision coverage; {incident} damage to the insured car is not covered.",
            )
    elif is_comprehensive_type:
        if comprehensive:
            return Finding(
                check="coverage_applies",
                status="green",
                reason=f"Covered under Comprehensive coverage (${deductible} deductible).",
            )
        else:
            return Finding(
                check="coverage_applies",
                status="red",
                reason=f"This policy does not carry comprehensive coverage; {incident} damage to the insured car is not covered.",
            )
    else:
        return Finding(
            check="coverage_applies",
            status="unchecked",
            reason=f"Incident type '{incident}' requires adjuster review for coverage applicability.",
        )


def check_vin_match(policy: Any, readings: Any) -> Finding:
    policy_vin = _get_val(policy, "vin")
    if not policy_vin or not str(policy_vin).strip():
        return Finding(
            check="vin_match",
            status="unchecked",
            reason="Policy does not have a VIN on file.",
        )

    if isinstance(readings, VinReading) or (hasattr(readings, "readable") and hasattr(readings, "vin")):
        vin_reading = readings
    else:
        vin_reading = _get_val(readings, "vin")

    if vin_reading is None:
        return Finding(
            check="vin_match",
            status="unchecked",
            reason="VIN photo reading is missing.",
        )

    readable = _get_val(vin_reading, "readable", False)
    reading_vin = _get_val(vin_reading, "vin")
    if not readable or not reading_vin or not str(reading_vin).strip():
        return Finding(
            check="vin_match",
            status="unchecked",
            reason="VIN is unreadable in photo.",
        )

    norm_policy = re.sub(r"[\s\-]+", "", str(policy_vin)).upper()
    norm_reading = re.sub(r"[\s\-]+", "", str(reading_vin)).upper()

    if norm_reading == norm_policy:
        return Finding(
            check="vin_match",
            status="green",
            reason="The VIN photo matches the policy.",
        )
    else:
        return Finding(
            check="vin_match",
            status="red",
            reason=f"The VIN photo ({norm_reading}) does not match the policy ({norm_policy}).",
        )


def check_photo_skipped(policy: Any) -> list[Finding]:
    findings = []
    skipped_slots = _get_val(policy, "skipped_slots") or {}
    if isinstance(skipped_slots, dict):
        for slot, reason in skipped_slots.items():
            if reason:
                findings.append(Finding(
                    check="photo_skipped",
                    status="amber",
                    reason=f"The claimant skipped {slot}: {reason}",
                ))
    return findings


def run_checks(
    policy: Any,
    readings: Any,
    photos: dict[str, str] | None = None,
    reused_photos: dict[str, str] | None = None,
    include_coverage: bool | None = None,
) -> list[Finding]:
    """Execute the plain-code checks comparing policy, AI readings, and photos."""
    findings: list[Finding] = []

    if include_coverage is None:
        include_coverage = bool(
            _get_val(policy, "policy_number")
            or _get_val(policy, "effective_date")
            or _get_val(policy, "vin")
            or _get_val(policy, "liability_only")
            or _get_val(policy, "collision")
            or _get_val(policy, "comprehensive")
        )

    if include_coverage:
        findings.append(check_policy_active(policy))
        findings.append(check_coverage_applies(policy))

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

    # 5b. vin_match
    if include_coverage:
        findings.append(check_vin_match(policy, readings))

    # 6. parts_match
    findings.append(check_parts_match(policy, readings))

    # 7. duplicate_photo & 8. photo_reused
    if photos is not None:
        findings.append(check_duplicate_photo(photos))
        findings.append(check_photo_reused(photos, reused_photos))

    # 9. photo_skipped
    for f in check_photo_skipped(policy):
        findings.append(f)

    return findings
