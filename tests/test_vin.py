import pytest
from witnessed.vin import is_valid_vin


def test_known_valid_vin():
    # Known valid VIN specified in task requirements
    assert is_valid_vin("1HGCM82633A004352") is True


def test_valid_vin_lowercase_and_whitespace():
    # Lowercase and whitespace should be normalized
    assert is_valid_vin("  1hgcm82633a004352  ") is True


def test_valid_vin_with_check_digit_x():
    # Valid VIN where check digit remainder is 10 ('X')
    assert is_valid_vin("11111111X21111111") is True


def test_invalid_vin_length():
    # Too short (16 chars)
    assert is_valid_vin("1HGCM82633A00435") is False
    # Too long (18 chars)
    assert is_valid_vin("1HGCM82633A0043529") is False
    # Empty string
    assert is_valid_vin("") is False


def test_invalid_vin_illegal_letters():
    # Contains 'I'
    assert is_valid_vin("1HGCM82633A00435I") is False
    # Contains 'O'
    assert is_valid_vin("1HGCM82633A00435O") is False
    # Contains 'Q'
    assert is_valid_vin("1HGCM82633A00435Q") is False


def test_invalid_vin_bad_check_digit():
    # Correct check digit is '3', replace with '4' or '0'
    assert is_valid_vin("1HGCM82643A004352") is False
    assert is_valid_vin("1HGCM82603A004352") is False


def test_invalid_vin_non_alphanumeric():
    assert is_valid_vin("1HGCM826-3A004352") is False
    assert is_valid_vin("1HGCM826 3A004352") is False
    assert is_valid_vin("1HGCM826!3A004352") is False


def test_invalid_vin_non_string():
    assert is_valid_vin(None) is False
    assert is_valid_vin(12345678901234567) is False
    assert is_valid_vin(["1HGCM82633A004352"]) is False
