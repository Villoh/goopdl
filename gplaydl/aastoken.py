"""Google account AAS token authentication without credential files."""

from __future__ import annotations

import hashlib
import secrets
import struct
from base64 import b64decode, urlsafe_b64encode

import httpx

AUTH_URL = "https://android.clients.google.com/auth"
GOOGLE_PUBKEY = (
    "AAAAgMom/1a/v0lblO2Ubrt60J2gcuXSljGFQXgcyZWveWLEwo6prwgi3iJIZdodyhKZQrNWp5nKJ3srRXcUW+F1BD3baEVGcmEgqaLZUNBjm057pKRI16kB0YppeGx5qIQ5QjKzsR8ETQbKLNWgRY0QRNVz34kMJR3P/LgHax/6rmf5AAAAAwEAAQ=="
)


class AASTokenError(ValueError):
    """Google rejected account authentication."""


def _mgf1(seed: bytes, length: int) -> bytes:
    return b"".join(
        hashlib.sha1(seed + counter.to_bytes(4, "big")).digest()
        for counter in range((length + 19) // 20)
    )[:length]


def encrypt_password(email: str, password: str) -> str:
    """Return Google-compatible RSA-OAEP encrypted credentials."""
    binary_key = b64decode(GOOGLE_PUBKEY)
    modulus_length = struct.unpack("!L", binary_key[:4])[0]
    modulus = int.from_bytes(binary_key[4 : 4 + modulus_length], "big")
    exponent_offset = 4 + modulus_length
    exponent_length = struct.unpack(
        "!L", binary_key[exponent_offset : exponent_offset + 4]
    )[0]
    exponent = int.from_bytes(
        binary_key[exponent_offset + 4 : exponent_offset + 4 + exponent_length],
        "big",
    )

    message = email.encode() + b"\x00" + password.encode()
    key_size = (modulus.bit_length() + 7) // 8
    if len(message) > key_size - 42:
        raise ValueError("Email and password are too long")

    label_hash = hashlib.sha1(b"").digest()
    data_block = (
        label_hash
        + b"\x00" * (key_size - len(message) - 42)
        + b"\x01"
        + message
    )
    seed = secrets.token_bytes(20)
    masked_data_block = bytes(
        left ^ right for left, right in zip(data_block, _mgf1(seed, key_size - 21))
    )
    masked_seed = bytes(
        left ^ right for left, right in zip(seed, _mgf1(masked_data_block, 20))
    )
    encoded = b"\x00" + masked_seed + masked_data_block
    encrypted = pow(int.from_bytes(encoded, "big"), exponent, modulus).to_bytes(
        key_size, "big"
    )
    return urlsafe_b64encode(
        b"\x00" + hashlib.sha1(binary_key).digest()[:4] + encrypted
    ).decode()


def fetch_aas_token(email: str, password_or_oauth_token: str) -> str:
    """Return an AAS token from a password or one-time EmbeddedSetup token."""
    oauth = password_or_oauth_token.startswith("oauth2_4/")
    if oauth:
        data = {
            "Email": email,
            "Token": password_or_oauth_token,
            "ACCESS_TOKEN": "1",
            "add_account": "1",
            "callerPkg": "com.google.android.gms",
            "callerSig": "38918a453d07199354f8b19af05ec6562ced5788",
            "device_country": "us",
            "droidguard_results": "null",
            "get_accountid": "1",
            "google_play_services_version": "240913000",
            "lang": "en",
            "sdk_version": "28",
            "service": "ac2dm",
        }
    else:
        data = {
            "Email": email,
            "EncryptedPasswd": encrypt_password(email, password_or_oauth_token),
            "add_account": "1",
            "accountType": "HOSTED_OR_GOOGLE",
            "google_play_services_version": "240913000",
            "has_permission": "1",
            "source": "android",
            "device_country": "us",
            "operatorCountry": "us",
            "lang": "en",
            "sdk_version": "17",
            "droidguard_results": "dummy123",
            "client_sig": "38918a453d07199354f8b19af05ec6562ced5788",
            "callerSig": "38918a453d07199354f8b19af05ec6562ced5788",
            "service": "ac2dm",
            "callerPkg": "com.google.android.gms",
        }

    response = httpx.post(
        AUTH_URL,
        data=data,
        headers={
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "" if oauth else "GoogleAuth/1.4",
            "app": "com.google.android.gms",
        },
        timeout=30,
    )

    values = {}
    for line in response.text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if values.get("Token"):
        return values["Token"]
    if values.get("Error"):
        reason = values["Error"]
        raise AASTokenError(reason if reason.isidentifier() else "Rejected")
    response.raise_for_status()
    raise AASTokenError("NoToken")
