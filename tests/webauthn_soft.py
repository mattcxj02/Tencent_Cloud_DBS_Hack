"""Software authenticator for tests only.

Generates ES256 (ECDSA P-256 SHA-256) credentials and produces
WebAuthn registration and authentication responses that the
``webauthn`` library can verify.  Uses ``cryptography`` for key
generation and signing, and ``webauthn.helpers.encode_cbor`` for
CBOR encoding (no new dependency).

**Never use this in production.**  It exists only for automated tests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import encode_cbor

__all__ = [
    "SoftCredential",
    "generate_credential",
    "create_registration_response",
    "create_authentication_response",
]


def _b64url_encode(raw: bytes) -> str:
    """Base64url-encode without padding (matching browser API)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(val: str) -> bytes:
    """Base64url-decode, adding padding if needed."""
    padded = val + "=" * (4 - len(val) % 4) if len(val) % 4 else val
    return base64.urlsafe_b64decode(padded)


@dataclass
class SoftCredential:
    """A software-generated WebAuthn credential for tests."""

    credential_id: bytes
    credential_id_b64: str
    private_key: ec.EllipticCurvePrivateKey
    public_key: bytes  # CBOR-encoded COSE key
    sign_count: int = 0

    def increment_sign_count(self) -> int:
        self.sign_count += 1
        return self.sign_count


def generate_credential() -> SoftCredential:
    """Generate a new ES256 software credential.

    Returns a :class:`SoftCredential` with the private key, credential
    ID, and CBOR-encoded COSE public key.
    """
    priv = ec.generate_private_key(ec.SECP256R1())
    pub = priv.public_key()
    pub_numbers = pub.public_numbers()

    # COSE_Key map (CBOR-encoded).
    # kty=2 (EC), alg=-7 (ES256), crv=1 (P-256), x, y
    cose_key = {
        1: 2,  # kty: EC
        3: -7,  # alg: ES256
        -1: 1,  # crv: P-256
        -2: pub_numbers.x.to_bytes(32, "big"),  # x
        -3: pub_numbers.y.to_bytes(32, "big"),  # y
    }
    public_key_cbor = encode_cbor(cose_key)

    credential_id = hashlib.sha256(encode_cbor(cose_key)).digest()[:32]

    return SoftCredential(
        credential_id=credential_id,
        credential_id_b64=_b64url_encode(credential_id),
        private_key=priv,
        public_key=public_key_cbor,
        sign_count=0,
    )


def _make_client_data(client_type: str, challenge: bytes, origin: str) -> bytes:
    """Build clientDataJSON bytes."""
    client_data = {
        "type": client_type,  # "webauthn.create" or "webauthn.get"
        "challenge": _b64url_encode(challenge),
        "origin": origin,
        "crossOrigin": False,
    }
    return json.dumps(client_data, separators=(",", ":")).encode("utf-8")


def _make_auth_data(
    rp_id: str,
    flags: int,
    sign_count: int,
    *,
    attested_credential_data: bytes | None = None,
) -> bytes:
    """Build authenticator data bytes.

    Structure: rp_id_hash (32) || flags (1) || sign_count (4) [|| attested_credential_data]
    """
    rp_id_hash = hashlib.sha256(rp_id.encode("ascii")).digest()
    count_bytes = struct.pack(">I", sign_count)
    data = rp_id_hash + bytes([flags]) + count_bytes
    if attested_credential_data is not None:
        data += attested_credential_data
    return data


def _make_attested_credential_data(credential_id: bytes, public_key_cbor: bytes) -> bytes:
    """Build attested credential data for registration.

    Structure: aaguid (16) || cred_id_len (2) || cred_id || COSE_key
    """
    aaguid = b"\x00" * 16  # zero aaguid (no attestation)
    cred_id_len = struct.pack(">H", len(credential_id))
    return aaguid + cred_id_len + credential_id + public_key_cbor


def create_registration_response(
    cred: SoftCredential,
    *,
    challenge: bytes,
    rp_id: str,
    origin: str,
    user_id: str,
) -> dict[str, Any]:
    """Build a WebAuthn registration response (credential dict).

    The returned dict matches the browser API format that
    ``verify_registration_response`` accepts.
    """
    # Flags: UP=1 (bit 0) | UV=1 (bit 2) | AT=1 (bit 6) = 0b01000101 = 0x45
    flags = 0x45

    attested_cred_data = _make_attested_credential_data(cred.credential_id, cred.public_key)
    auth_data = _make_auth_data(
        rp_id, flags, cred.sign_count, attested_credential_data=attested_cred_data
    )

    client_data = _make_client_data("webauthn.create", challenge, origin)

    # Attestation object (fmt=none, auth_data, no att_stmt).
    attestation_object = encode_cbor(
        {
            "fmt": "none",
            "attStmt": {},
            "authData": auth_data,
        }
    )

    return {
        "id": cred.credential_id_b64,
        "rawId": cred.credential_id_b64,
        "response": {
            "clientDataJSON": _b64url_encode(client_data),
            "attestationObject": _b64url_encode(attestation_object),
            "transports": ["internal"],
        },
        "type": "public-key",
    }


def create_authentication_response(
    cred: SoftCredential,
    *,
    challenge: bytes,
    rp_id: str,
    origin: str,
    flags: int = 0x05,
    sign_count: int | None = None,
) -> dict[str, Any]:
    """Build a WebAuthn authentication response (assertion dict).

    ``flags`` defaults to UP|UV (0x05); pass 0x01 for user presence without
    verification. ``sign_count`` overrides the incrementing counter (e.g. 0 for
    authenticators that do not count, or a stale value to test rejection).

    The returned dict matches the browser API format that
    ``verify_authentication_response`` accepts.
    """
    # Increment sign count for the new assertion.
    new_count = cred.increment_sign_count() if sign_count is None else sign_count

    auth_data = _make_auth_data(rp_id, flags, new_count)
    client_data = _make_client_data("webauthn.get", challenge, origin)

    # Sign over SHA256(auth_data || client_data_hash).
    client_data_hash = hashlib.sha256(client_data).digest()
    signed_data = auth_data + client_data_hash
    signature = cred.private_key.sign(signed_data, ec.ECDSA(hashes.SHA256()))

    # The signature is in DER format; the webauthn library expects raw
    # r||s format.  Let's check — actually, webauthn.helpers.verify_signature
    # handles DER-encoded signatures for ECDSA.  Let's keep it as DER.

    return {
        "id": cred.credential_id_b64,
        "rawId": cred.credential_id_b64,
        "response": {
            "clientDataJSON": _b64url_encode(client_data),
            "authenticatorData": _b64url_encode(auth_data),
            "signature": _b64url_encode(signature),
        },
        "type": "public-key",
    }
