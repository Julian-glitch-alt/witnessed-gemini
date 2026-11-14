import pytest
from witnessed.checks import map_parts_to_claimed_area, run_checks
from witnessed.schemas import (
    DamageReading,
    Finding,
    PlateReading,
    Policy,
    VinReading,
    WideReading,
)


@pytest.fixture
def clean_policy():
    return Policy(
        make="Toyota",
        model="Camry",
        color="Blue",
        plate="7XYZ123",
        state="CA",
        claimant_name="Jane Doe",
        claimant_statement="I was rear-ended at a red light.",
        claimed_damage_area="rear",
        claimed_parts=["Rear bumper"],
    )


@pytest.fixture
def clean_readings():
    return {
        "wide": WideReading(
            make="Toyota",
            model="Camry",
            color="Blue",
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
        "plate": PlateReading(
            plate_text="7XYZ123",
            state="CA",
            readable=True,
        ),
        "vin": VinReading(
            vin="1HGCM82633A004352",
            readable=True,
        ),
    }


def _finding_map(findings: list[Finding]) -> dict[str, Finding]:
    return {f.check: f for f in findings}


def test_all_green_case(clean_policy, clean_readings):
    findings = run_checks(clean_policy, clean_readings)
    f_map = _finding_map(findings)

    assert f_map["plate_match"].status == "green"
    assert f_map["vehicle_match"].status == "green"
    assert f_map["damage_location"].status == "green"
    assert f_map["damage_present"].status == "green"
    assert f_map["vin_format"].status == "green"


def test_spec_demo_wrong_plate_is_red(clean_policy, clean_readings):
    # SPEC demo case 1: wrong plate -> red
    clean_readings["plate"] = PlateReading(
        plate_text="9ZZZ999",
        state="CA",
        readable=True,
    )
    findings = run_checks(clean_policy, clean_readings)
    f_map = _finding_map(findings)

    assert f_map["plate_match"].status == "red"
    assert "does not match policy" in f_map["plate_match"].reason


def test_spec_demo_front_damage_with_rear_claimed_is_amber(clean_policy, clean_readings):
    # SPEC demo case 2: front damage with rear claimed -> amber
    clean_readings["damage"] = DamageReading(
        damage_visible=True,
        damage_type="crush",
        damage_area="front",
        severity="moderate",
    )
    clean_readings["wide"] = WideReading(
        make="Toyota",
        model="Camry",
        color="Blue",
        damage_visible=True,
        damage_area="front",
    )
    findings = run_checks(clean_policy, clean_readings)
    f_map = _finding_map(findings)

    assert f_map["damage_location"].status == "amber"
    assert "Damage observed at front, but claimed damage area is rear" in f_map["damage_location"].reason


def test_plate_match_paths(clean_policy, clean_readings):
    # Normalization: spaces and dashes stripped, case insensitive
    clean_policy.plate = "7xyz-123"
    clean_readings["plate"] = PlateReading(plate_text=" 7XYZ 123 ", state="CA", readable=True)
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["plate_match"].status == "green"

    # Unreadable -> unchecked
    clean_readings["plate"] = PlateReading(plate_text=None, state=None, readable=False)
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["plate_match"].status == "unchecked"

    # Missing reading -> unchecked
    clean_readings["plate"] = None
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["plate_match"].status == "unchecked"

    # Missing policy plate -> unchecked
    clean_policy.plate = None
    clean_readings["plate"] = PlateReading(plate_text="7XYZ123", readable=True)
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["plate_match"].status == "unchecked"


def test_vehicle_match_paths(clean_policy, clean_readings):
    # Make mismatch -> amber
    clean_readings["wide"] = WideReading(make="Honda", color="Blue")
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["vehicle_match"].status == "amber"

    # Color mismatch -> amber
    clean_readings["wide"] = WideReading(make="Toyota", color="Red")
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["vehicle_match"].status == "amber"

    # Unsure make / color -> unchecked (never green)
    clean_readings["wide"] = WideReading(make="Toyota", color="unsure")
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["vehicle_match"].status == "unchecked"

    clean_readings["wide"] = WideReading(make="unsure", color="unsure")
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["vehicle_match"].status == "unchecked"

    # Missing reading -> unchecked
    clean_readings["wide"] = None
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["vehicle_match"].status == "unchecked"


def test_damage_location_paths(clean_policy, clean_readings):
    # Match -> green
    clean_policy.claimed_damage_area = "rear bumper"
    clean_readings["damage"] = DamageReading(damage_visible=True, damage_area="rear")
    clean_readings["wide"] = WideReading(damage_area="unsure")
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["damage_location"].status == "green"

    # Mismatch -> amber
    clean_policy.claimed_damage_area = "rear"
    clean_readings["damage"] = DamageReading(damage_visible=True, damage_area="left")
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["damage_location"].status == "amber"

    # Conflicting locations -> amber
    clean_policy.claimed_damage_area = "rear"
    clean_readings["damage"] = DamageReading(damage_visible=True, damage_area="rear")
    clean_readings["wide"] = WideReading(damage_area="front")
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["damage_location"].status == "amber"

    # Unsure / missing -> unchecked
    clean_readings["damage"] = DamageReading(damage_visible=True, damage_area="unsure")
    clean_readings["wide"] = WideReading(damage_area="none")
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["damage_location"].status == "unchecked"


def test_damage_present_paths(clean_policy, clean_readings):
    # Damage visible -> green
    clean_readings["damage"] = DamageReading(damage_visible=True, damage_type="scratch")
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["damage_present"].status == "green"

    # No damage -> amber
    clean_readings["damage"] = DamageReading(damage_visible=False)
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["damage_present"].status == "amber"

    # Missing damage reading -> unchecked
    clean_readings["damage"] = None
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["damage_present"].status == "unchecked"


def test_vin_format_paths(clean_policy, clean_readings):
    # Valid VIN -> green
    clean_readings["vin"] = VinReading(vin="1HGCM82633A004352", readable=True)
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["vin_format"].status == "green"

    # Invalid check digit -> amber
    clean_readings["vin"] = VinReading(vin="1HGCM82643A004352", readable=True)
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["vin_format"].status == "amber"

    # Unreadable VIN -> unchecked
    clean_readings["vin"] = VinReading(vin=None, readable=False)
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["vin_format"].status == "unchecked"

    # Missing reading -> unchecked
    clean_readings["vin"] = None
    findings = run_checks(clean_policy, clean_readings)
    assert _finding_map(findings)["vin_format"].status == "unchecked"


def test_all_missing_readings_give_all_unchecked(clean_policy):
    # Rule: A missing or unchecked reading gives unchecked, never green.
    findings = run_checks(clean_policy, {})
    assert len(findings) == 6
    for f in findings:
        assert f.status == "unchecked"


def test_vehicle_match_color_families_and_punctuation():
    # Make with punctuation and casing
    policy = Policy(
        make="Mercedes-Benz",
        model="C-Class",
        color="dark blue",
        plate="ABC1234",
        claimed_damage_area="rear",
    )
    readings = {
        "wide": WideReading(make="mercedes benz", color="navy"),
    }
    findings = run_checks(policy, readings)
    assert _finding_map(findings)["vehicle_match"].status == "green"

    # Gray family: silver vs grey, gray
    policy.color = "silver"
    readings["wide"] = WideReading(make="Mercedes-Benz", color="grey")
    findings = run_checks(policy, readings)
    assert _finding_map(findings)["vehicle_match"].status == "green"

    readings["wide"] = WideReading(make="Mercedes-Benz", color="gray")
    findings = run_checks(policy, readings)
    assert _finding_map(findings)["vehicle_match"].status == "green"

    # White family: white vs pearl
    policy.color = "pearl"
    readings["wide"] = WideReading(make="Mercedes-Benz", color="white")
    findings = run_checks(policy, readings)
    assert _finding_map(findings)["vehicle_match"].status == "green"

    # Red family: red vs maroon
    policy.color = "maroon"
    readings["wide"] = WideReading(make="Mercedes-Benz", color="red")
    findings = run_checks(policy, readings)
    assert _finding_map(findings)["vehicle_match"].status == "green"

    # Other colors: yellow vs yellow
    policy.color = "yellow"
    readings["wide"] = WideReading(make="Mercedes-Benz", color="yellow")
    findings = run_checks(policy, readings)
    assert _finding_map(findings)["vehicle_match"].status == "green"

    # Other colors mismatch: yellow vs green
    readings["wide"] = WideReading(make="Mercedes-Benz", color="green")
    findings = run_checks(policy, readings)
    assert _finding_map(findings)["vehicle_match"].status == "amber"


def test_duplicate_photo_all_distinct_is_green(clean_policy, clean_readings):
    photos = {
        "wide": "hash_111",
        "damage": "hash_222",
        "plate": "hash_333",
        "vin": "hash_444",
    }
    findings = run_checks(clean_policy, clean_readings, photos=photos)
    f_map = _finding_map(findings)
    assert "duplicate_photo" in f_map
    assert f_map["duplicate_photo"].status == "green"
    assert "distinct" in f_map["duplicate_photo"].reason.lower()


def test_duplicate_photo_two_slots_duplicate_is_amber(clean_policy, clean_readings):
    photos = {
        "wide": "same_hash",
        "damage": "same_hash",
        "plate": "hash_333",
        "vin": "hash_444",
    }
    findings = run_checks(clean_policy, clean_readings, photos=photos)
    f_map = _finding_map(findings)
    assert "duplicate_photo" in f_map
    assert f_map["duplicate_photo"].status == "amber"
    assert f_map["duplicate_photo"].reason == "The same image was submitted for slots wide and damage."


def test_duplicate_photo_plate_and_vin_duplicate_is_amber(clean_policy, clean_readings):
    photos = {
        "wide": "hash_111",
        "damage": "hash_222",
        "plate": "shared_hash",
        "vin": "shared_hash",
    }
    findings = run_checks(clean_policy, clean_readings, photos=photos)
    f_map = _finding_map(findings)
    assert "duplicate_photo" in f_map
    assert f_map["duplicate_photo"].status == "amber"
    assert f_map["duplicate_photo"].reason == "The same image was submitted for slots plate and vin."


def test_photo_reused_no_prior_claims_is_green(clean_policy, clean_readings):
    photos = {
        "wide": "hash_111",
        "damage": "hash_222",
        "plate": "hash_333",
        "vin": "hash_444",
    }
    findings = run_checks(clean_policy, clean_readings, photos=photos, reused_photos={})
    f_map = _finding_map(findings)
    assert "photo_reused" in f_map
    assert f_map["photo_reused"].status == "green"
    assert "no submitted photos appear" in f_map["photo_reused"].reason.lower()


def test_photo_reused_matching_other_claim_is_red(clean_policy, clean_readings):
    photos = {
        "wide": "hash_111",
        "damage": "shared_hash_999",
        "plate": "hash_333",
        "vin": "hash_444",
    }
    reused = {"shared_hash_999": "CLM-PRIOR01"}
    findings = run_checks(clean_policy, clean_readings, photos=photos, reused_photos=reused)
    f_map = _finding_map(findings)
    assert "photo_reused" in f_map
    assert f_map["photo_reused"].status == "red"
    assert f_map["photo_reused"].reason == "This exact image was already submitted in claim CLM-PRIOR01."


def test_map_parts_to_claimed_area_returns_none_when_no_part_maps():
    assert map_parts_to_claimed_area(["Roof"]) is None
    assert map_parts_to_claimed_area(["Wheels or tires"]) is None
    assert map_parts_to_claimed_area(["Roof", "Wheels or tires"]) is None
    assert map_parts_to_claimed_area(["Inside the car", "Underneath"]) is None
    assert map_parts_to_claimed_area([]) is None
    assert map_parts_to_claimed_area(["Roof"]) != "rear"


def test_map_parts_to_claimed_area_maps_expected_areas():
    assert map_parts_to_claimed_area(["Front bumper"]) == "front"
    assert map_parts_to_claimed_area(["Rear bumper"]) == "rear"
    assert map_parts_to_claimed_area(["Left side"]) == "left"
    assert map_parts_to_claimed_area(["Right side"]) == "right"


def test_damage_location_check_reports_unchecked_when_claimed_area_is_none(clean_policy, clean_readings):
    claimed_area = map_parts_to_claimed_area(["Roof"])
    assert claimed_area is None
    clean_policy.claimed_damage_area = claimed_area
    findings = run_checks(clean_policy, clean_readings)
    f_map = _finding_map(findings)
    assert f_map["damage_location"].status == "unchecked"
    assert "not specified" in f_map["damage_location"].reason.lower()




