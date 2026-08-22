"""Transport factory — selects the active transport from environment.

Reads ``CLIPTUNNEL_TRANSPORT`` (default ``clipboard``, case-insensitive).
``clipboard`` → :class:`~cliptunnel_mcp.clipboard_transport.ClipboardTransport`.
``https`` → :class:`~cliptunnel_mcp.https_transport.HttpsTransport`.

All imports are lazy (inside the function body) so importing this module
never pulls in ``clipboard-event`` or ``cryptography``.
"""
from __future__ import annotations

import os

from cliptunnel_mcp.transport import Transport

__all__ = ["build_transport"]

_ACCEPTED = {"clipboard", "https"}


def build_transport() -> Transport:
    """Build the transport selected by ``CLIPTUNNEL_TRANSPORT``.

    Returns a :class:`Transport` (which also implements
    :class:`RevisionMonitor`).

    Raises :class:`ValueError` for unknown transport selectors or missing
    required environment variables.
    """
    choice = os.environ.get("CLIPTUNNEL_TRANSPORT", "clipboard").strip().lower()

    if choice == "clipboard":
        from cliptunnel_mcp.clipboard_transport import ClipboardTransport

        return ClipboardTransport()

    if choice == "https":
        repeater_url = os.environ.get("CLIPTUNNEL_REPEATER_URL", "").strip()
        bearer_token = os.environ.get("CLIPTUNNEL_REPEATER_TOKEN", "").strip()

        missing: list[str] = []
        if not repeater_url:
            missing.append("CLIPTUNNEL_REPEATER_URL")
        if not bearer_token:
            missing.append("CLIPTUNNEL_REPEATER_TOKEN")
        if missing:
            raise ValueError(
                "CLIPTUNNEL_TRANSPORT=https requires: "
                + ", ".join(missing)
            )

        aes_key: bytes | None = None
        aes_env = os.environ.get("CLIPTUNNEL_AES_KEY")
        if aes_env:
            from cliptunnel_mcp.crypto import parse_key

            aes_key = parse_key(aes_env)  # raises ValueError on bad key

        from cliptunnel_mcp.https_transport import HttpsTransport

        return HttpsTransport(
            repeater_url=repeater_url,
            bearer_token=bearer_token,
            aes_key=aes_key,
        )

    raise ValueError(
        f"CLIPTUNNEL_TRANSPORT='{choice}' is not supported. "
        f"Accepted values: {', '.join(sorted(_ACCEPTED))}"
    )