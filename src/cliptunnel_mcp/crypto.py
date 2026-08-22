"""AES-256-GCM encryption layer for the HTTPS transport.

When ``CLIPTUNNEL_AES_KEY`` is set, :class:`HttpsTransport` uses these helpers
to encrypt the full CT3 wire string before writing it to the repeater and to
decrypt it after reading.  The repeater never sees plaintext.

Wire format::

    base64( nonce[12] || AES-256-GCM(key, nonce, plaintext) )

AES-GCM provides confidentiality + integrity in one operation: the 16-byte
authentication tag is appended to the ciphertext by ``AESGCM.encrypt`` and
verified by ``AESGCM.decrypt``.  Tampering with any byte causes decryption
to raise ``InvalidTag``.
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = ["encrypt", "decrypt", "parse_key"]

# AES-256 requires a 32-byte key.
_KEY_LEN = 32
# 12-byte nonce is the GCM recommended size.
_NONCE_LEN = 12


def encrypt(plaintext: str, key: bytes) -> str:
    """Encrypt *plaintext* with AES-256-GCM.

    :param plaintext: UTF-8 string to encrypt.
    :param key: 32-byte AES-256 key.
    :return: base64 string of ``nonce || ciphertext+tag``.
    :raises ValueError: if *key* is not 32 bytes.
    """
    if len(key) != _KEY_LEN:
        raise ValueError(f"AES-256 key must be {_KEY_LEN} bytes, got {len(key)}")

    nonce = os.urandom(_NONCE_LEN)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    blob = nonce + ct
    return base64.b64encode(blob).decode("ascii")


def decrypt(blob: str, key: bytes) -> str:
    """Decrypt a base64 blob produced by :func:`encrypt`.

    :param blob: base64 string of ``nonce || ciphertext+tag``.
    :param key: 32-byte AES-256 key.
    :return: the original UTF-8 plaintext.
    :raises ValueError: if *key* is not 32 bytes or the blob is too short.
    :raises Exception: if the tag verification fails (tampered or wrong key).
    """
    if len(key) != _KEY_LEN:
        raise ValueError(f"AES-256 key must be {_KEY_LEN} bytes, got {len(key)}")

    raw = base64.b64decode(blob)  # raises binascii.Error on invalid base64
    if len(raw) < _NONCE_LEN:
        raise ValueError("encrypted blob too short to contain a nonce")
    nonce = raw[:_NONCE_LEN]
    ct = raw[_NONCE_LEN:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ct, associated_data=None)
    return plaintext.decode("utf-8")


def parse_key(raw: str) -> bytes:
    """Parse a base64-encoded AES-256 key from the ``CLIPTUNNEL_AES_KEY`` env var.

    :param raw: base64 string encoding 32 bytes.
    :return: 32-byte key.
    :raises ValueError: if the value is empty, not valid base64, or not 32 bytes.
    """
    if not raw:
        raise ValueError("CLIPTUNNEL_AES_KEY is empty")
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:
        raise ValueError(f"CLIPTUNNEL_AES_KEY is not valid base64: {exc}") from exc
    if len(key) != _KEY_LEN:
        raise ValueError(
            f"CLIPTUNNEL_AES_KEY must decode to {_KEY_LEN} bytes, got {len(key)}"
        )
    return key