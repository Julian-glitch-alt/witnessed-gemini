VIN_TRANSLITERATION: dict[str, int] = {
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
}

VIN_WEIGHTS: list[int] = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def is_valid_vin(vin: str | None) -> bool:
    """Validate 17-character VIN with ISO 3779 check digit calculation."""
    if not isinstance(vin, str):
        return False

    v = vin.strip().upper()
    if len(v) != 17:
        return False

    # ISO 3779 forbids letters I, O, and Q to prevent confusion with 1, 0
    if any(ch in v for ch in ("I", "O", "Q")):
        return False

    # Check all characters belong to the transliteration table
    if not all(ch in VIN_TRANSLITERATION for ch in v):
        return False

    total = sum(VIN_TRANSLITERATION[ch] * weight for ch, weight in zip(v, VIN_WEIGHTS))
    remainder = total % 11
    expected_check = "X" if remainder == 10 else str(remainder)

    return v[8] == expected_check
