import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def canonical_json(data: Any) -> bytes:
    """Serialize data into canonical JSON bytes with sorted keys and no whitespace."""
    if hasattr(data, "model_dump"):
        data = data.model_dump()
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def load_or_create_key(key_path: str | Path | None = None) -> ed25519.Ed25519PrivateKey:
    """Load an existing Ed25519 private key or generate and save a new one."""
    path_str = key_path or os.environ.get("SEAL_KEY_PATH")
    if not path_str:
        path_str = str(Path.home() / ".witnessed" / "seal_key.pem")

    path = Path(path_str)
    if path.exists():
        with open(path, "rb") as f:
            loaded_key = serialization.load_pem_private_key(f.read(), password=None)
            if not isinstance(loaded_key, ed25519.Ed25519PrivateKey):
                raise ValueError("Loaded key is not an Ed25519 private key")
            return loaded_key

    path.parent.mkdir(parents=True, exist_ok=True)
    private_key = ed25519.Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(path, "wb") as f:
        f.write(pem)

    return private_key


def seal(
    package: Any, private_key: ed25519.Ed25519PrivateKey | None = None
) -> tuple[bytes, str]:
    """Seal a package into canonical JSON bytes and an Ed25519 base64 signature."""
    if isinstance(package, bytes):
        package_bytes = package
    else:
        package_bytes = canonical_json(package)

    key = private_key or load_or_create_key()
    signature = key.sign(package_bytes)
    signature_b64 = base64.b64encode(signature).decode("ascii")
    return package_bytes, signature_b64


def verify(
    package_bytes: bytes,
    signature_b64: str | bytes,
    public_key: (
        ed25519.Ed25519PublicKey
        | ed25519.Ed25519PrivateKey
        | bytes
        | str
        | None
    ) = None,
) -> bool:
    """Verify an Ed25519 signature over canonical package bytes."""
    try:
        if public_key is None:
            pub: ed25519.Ed25519PublicKey = load_or_create_key().public_key()
        elif isinstance(public_key, ed25519.Ed25519PrivateKey):
            pub = public_key.public_key()
        elif isinstance(public_key, ed25519.Ed25519PublicKey):
            pub = public_key
        elif isinstance(public_key, str):
            try:
                raw = base64.b64decode(public_key)
                if len(raw) == 32:
                    pub = ed25519.Ed25519PublicKey.from_public_bytes(raw)
                else:
                    loaded = serialization.load_pem_public_key(public_key.encode("utf-8"))
                    if not isinstance(loaded, ed25519.Ed25519PublicKey):
                        return False
                    pub = loaded
            except Exception:
                loaded = serialization.load_pem_public_key(public_key.encode("utf-8"))
                if not isinstance(loaded, ed25519.Ed25519PublicKey):
                    return False
                pub = loaded
        elif isinstance(public_key, bytes):
            if len(public_key) == 32:
                pub = ed25519.Ed25519PublicKey.from_public_bytes(public_key)
            else:
                loaded = serialization.load_pem_public_key(public_key)
                if not isinstance(loaded, ed25519.Ed25519PublicKey):
                    return False
                pub = loaded
        else:
            return False

        if isinstance(signature_b64, str):
            sig_bytes = base64.b64decode(signature_b64)
        elif isinstance(signature_b64, bytes):
            if len(signature_b64) == 64:
                sig_bytes = signature_b64
            else:
                sig_bytes = base64.b64decode(signature_b64)
        else:
            return False

        pub.verify(sig_bytes, package_bytes)
        return True
    except (InvalidSignature, Exception):
        return False
