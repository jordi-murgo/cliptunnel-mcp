"""Transport factory — selects the active transport from configuration.

Settings resolve with precedence: environment variable > config file
(``~/.cliptunnel/config.toml``, see :mod:`cliptunnel_mcp.config`) >
built-in default. ``CLIPTUNNEL_TRANSPORT`` selects ``clipboard``
(default, case-insensitive), ``https``, or ``firebase``.
``clipboard`` → :class:`~cliptunnel_mcp.clipboard_transport.ClipboardTransport`.
``https`` → :class:`~cliptunnel_mcp.https_transport.HttpsTransport`.
``firebase`` → :class:`~cliptunnel_mcp.firebase_transport.FirebaseTransport`.

If ``CLIPTUNNEL_AES_KEY`` is set (base64 of 32 bytes), the selected transport
is wrapped in :class:`~cliptunnel_mcp.encrypted_transport.EncryptedTransport`
so that all values are encrypted with AES-256-GCM before entering the
transport and decrypted on read. This works with any transport.

All imports are lazy (inside the function body) so importing this module
never pulls in ``clipboard-event`` or ``cryptography`` at import time.
"""
from __future__ import annotations

from cliptunnel_mcp import config
from cliptunnel_mcp.transport import Transport

__all__ = ["build_transport"]
_ACCEPTED = {"clipboard", "https", "firebase"}


def build_transport() -> Transport:
    """Build the transport selected by ``CLIPTUNNEL_TRANSPORT`` (or its
    ``[transport] type`` config-file equivalent).

    If ``CLIPTUNNEL_AES_KEY`` is set, the transport is wrapped in
    :class:`~cliptunnel_mcp.encrypted_transport.EncryptedTransport`.

    Raises :class:`ValueError` for unknown transport selectors or missing
    required settings.
    """
    choice = config.get_env("CLIPTUNNEL_TRANSPORT", "clipboard").strip().lower()

    # --- Select the base transport ---
    if choice == "clipboard":
        from cliptunnel_mcp.clipboard_transport import ClipboardTransport

        transport: Transport = ClipboardTransport()

    elif choice == "https":
        from urllib.parse import urlparse

        repeater_url = (config.get_env("CLIPTUNNEL_REPEATER_URL") or "").strip()
        bearer_token = (config.get_env("CLIPTUNNEL_REPEATER_TOKEN") or "").strip()

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

    elif choice == "firebase":
        from urllib.parse import urlparse

        database_url = (config.get_env("CLIPTUNNEL_FIREBASE_URL") or "").strip()
        auth_token = (config.get_env("CLIPTUNNEL_FIREBASE_TOKEN") or "").strip()

        fb_missing: list[str] = []
        if not database_url:
            fb_missing.append("CLIPTUNNEL_FIREBASE_URL")
        if not auth_token:
            fb_missing.append("CLIPTUNNEL_FIREBASE_TOKEN")
        if fb_missing:
            raise ValueError(
                "CLIPTUNNEL_TRANSPORT=firebase requires: " + ", ".join(fb_missing)
            )

        if urlparse(database_url).scheme.lower() != "https":
            raise ValueError(
                "CLIPTUNNEL_FIREBASE_URL must use the https scheme "
                f"(got: {database_url!r})"
            )

        from cliptunnel_mcp.firebase_transport import FirebaseTransport

        transport = FirebaseTransport(
            database_url=database_url,
            auth_token=auth_token,
        )

    else:
        raise ValueError(
            f"CLIPTUNNEL_TRANSPORT='{choice}' is not supported. "
            f"Accepted values: {', '.join(sorted(_ACCEPTED))}"
        )

    # --- Optional AES encryption layer (works with any transport) ---
    aes_env = config.get_env("CLIPTUNNEL_AES_KEY")
    if aes_env:
        from cliptunnel_mcp.crypto import parse_key

        aes_key = parse_key(aes_env)  # raises ValueError on bad key

        from cliptunnel_mcp.encrypted_transport import EncryptedTransport

        transport = EncryptedTransport(transport, aes_key)

    return transport