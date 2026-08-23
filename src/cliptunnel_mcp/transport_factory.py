"""Transport factory — selects the active transport from environment.

Reads ``CLIPTUNNEL_TRANSPORT`` (default ``clipboard``, case-insensitive).
``clipboard`` → :class:`~cliptunnel_mcp.clipboard_transport.ClipboardTransport`.
``https`` → :class:`~cliptunnel_mcp.https_transport.HttpsTransport`.

If ``CLIPTUNNEL_AES_KEY`` is set (base64 of 32 bytes), the selected transport
is wrapped in :class:`~cliptunnel_mcp.encrypted_transport.EncryptedTransport`
so that all values are encrypted with AES-256-GCM before entering the
transport and decrypted on read. This works with any transport.

All imports are lazy (inside the function body) so importing this module
never pulls in ``clipboard-event`` or ``cryptography`` at import time.
"""
from __future__ import annotations

import os

from cliptunnel_mcp.transport import Transport

__all__ = ["build_transport"]

_ACCEPTED = {"clipboard", "https"}


def build_transport() -> Transport:
    """Build the transport selected by ``CLIPTUNNEL_TRANSPORT``.

    If ``CLIPTUNNEL_AES_KEY`` is set, the transport is wrapped in
    :class:`~cliptunnel_mcp.encrypted_transport.EncryptedTransport`.

    Raises :class:`ValueError` for unknown transport selectors or missing
    required environment variables.
    """
    choice = os.environ.get("CLIPTUNNEL_TRANSPORT", "clipboard").strip().lower()

    # --- Select the base transport ---
    if choice == "clipboard":
        from cliptunnel_mcp.clipboard_transport import ClipboardTransport

        transport: Transport = ClipboardTransport()

    elif choice == "https":
        from urllib.parse import urlparse

        repeater_url = os.environ.get("CLIPTUNNEL_REPEATER_URL", "").strip()
        bearer_token = os.environ.get("CLIPTUNNEL_REPEATER_TOKEN", "").strip()

        missing: list[str] = []
        if not repeater_url:
            missing.append("CLIPTUNNEL_REPEATER_URL")
        if not bearer_token:
            missing.append("CLIPTUNNEL_REPEATER_TOKEN")
        if missing:
            raise ValueError(
                "CLIPTUNNEL_TRANSPORT=https requires: " + ", ".join(missing)
            )

        if urlparse(repeater_url).scheme.lower() != "https":
            raise ValueError(
                "CLIPTUNNEL_REPEATER_URL must use the https scheme "
                f"(got: {repeater_url!r})"
            )

        from cliptunnel_mcp.https_transport import HttpsTransport

        transport = HttpsTransport(
            repeater_url=repeater_url,
            bearer_token=bearer_token,
        )

    else:
        raise ValueError(
            f"CLIPTUNNEL_TRANSPORT='{choice}' is not supported. "
            f"Accepted values: {', '.join(sorted(_ACCEPTED))}"
        )

    # --- Optional AES encryption layer (works with any transport) ---
    aes_env = os.environ.get("CLIPTUNNEL_AES_KEY")
    if aes_env:
        from cliptunnel_mcp.crypto import parse_key

        aes_key = parse_key(aes_env)  # raises ValueError on bad key

        from cliptunnel_mcp.encrypted_transport import EncryptedTransport

        transport = EncryptedTransport(transport, aes_key)

    return transport