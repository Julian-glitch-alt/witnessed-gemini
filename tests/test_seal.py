import base64
from pathlib import Path
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from witnessed.seal import (
    canonical_json,
    load_or_create_key,
    seal,
    verify,
)
from witnessed.schemas import Policy


def test_canonical_json_sorting_and_whitespace():
    data = {
        "z": 1,
        "a": 2,
        "m": {"b": 3, "a": 4},
    }
    canonical = canonical_json(data)
    # Must sort keys and have no spaces after colons/commas
    assert canonical == b'{"a":2,"m":{"a":4,"b":3},"z":1}'


def test_canonical_json_with_pydantic_model():
    policy = Policy(make="Toyota", model="Camry", color="Blue")
    canonical = canonical_json(policy)
    assert b'"make":"Toyota"' in canonical
    assert b" " not in canonical.replace(b'"Toyota"', b'').replace(b'"Camry"', b'').replace(b'"Blue"', b'')


def test_load_or_create_key_persistence(tmp_path: Path):
    key_file = tmp_path / "keys" / "test_key.pem"
    key1 = load_or_create_key(key_file)
    assert key_file.exists()

    # Second load retrieves the exact same private key
    key2 = load_or_create_key(key_file)
    pub1 = key1.public_key().public_bytes_raw()
    pub2 = key2.public_key().public_bytes_raw()
    assert pub1 == pub2


def test_seal_round_trip_passes(tmp_path: Path):
    key_file = tmp_path / "test_seal.pem"
    key = load_or_create_key(key_file)

    package = {
        "claim_id": "CLM-001",
        "session_code": "K7-42",
        "photos": {
            "wide": "hash1",
            "damage": "hash2",
            "plate": "hash3",
            "vin": "hash4",
        },
    }

    pkg_bytes, sig_b64 = seal(package, private_key=key)
    assert isinstance(pkg_bytes, bytes)
    assert isinstance(sig_b64, str)

    # Verify with Ed25519PublicKey object
    assert verify(pkg_bytes, sig_b64, key.public_key()) is True

    # Verify with raw 32-byte public key base64 string
    pub_b64 = base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")
    assert verify(pkg_bytes, sig_b64, pub_b64) is True


def test_seal_one_byte_change_fails(tmp_path: Path):
    key_file = tmp_path / "test_tamper.pem"
    key = load_or_create_key(key_file)

    package = {"claim_id": "CLM-001", "status": "approved"}
    pkg_bytes, sig_b64 = seal(package, private_key=key)

    # Modify exactly one byte of package_bytes
    tampered_bytes = bytearray(pkg_bytes)
    tampered_bytes[0] ^= 0x01  # Flip one bit in first byte
    tampered = bytes(tampered_bytes)

    assert verify(tampered, sig_b64, key.public_key()) is False


def test_corrupt_signature_fails(tmp_path: Path):
    key_file = tmp_path / "test_sig_fail.pem"
    key = load_or_create_key(key_file)

    package = {"claim_id": "CLM-001"}
    pkg_bytes, sig_b64 = seal(package, private_key=key)

    # Corrupt signature string
    corrupt_sig = sig_b64[:-2] + "AA"
    assert verify(pkg_bytes, corrupt_sig, key.public_key()) is False

    # Invalid base64 signature
    assert verify(pkg_bytes, "not_valid_b64!", key.public_key()) is False


def test_verify_with_different_key_fails(tmp_path: Path):
    key1 = load_or_create_key(tmp_path / "k1.pem")
    key2 = load_or_create_key(tmp_path / "k2.pem")

    package = {"session": "XYZ-99"}
    pkg_bytes, sig_b64 = seal(package, private_key=key1)

    assert verify(pkg_bytes, sig_b64, key2.public_key()) is False
