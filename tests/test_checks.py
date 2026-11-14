import pytest
from witnessed.checks import run_checks
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
        ),
        "damage": DamageReading(
            damage_visible=True,
            damage_type="crush",
            damage_area="rear",
            severity="moderate",
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
    assert len(findings) == 5
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

