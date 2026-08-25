"""Transport factory — selects the active transport from configuration.

Settings resolve with precedence: environment variable > config file
(``~/.cliptunnel/config.toml``, see :mod:`cliptunnel_mcp.config`) >
built-in default. ``CLIPTUNNEL_TRANSPORT`` selects ``clipboard``
(default, case-insensitive), ``https``, ``firebase``, or ``websocket``.
``clipboard`` → :class:`~cliptunnel_mcp.clipboard_transport.ClipboardTransport`.
``https`` → :class:`~cliptunnel_mcp.https_transport.HttpsTransport`.
``firebase`` → :class:`~cliptunnel_mcp.firebase_transport.FirebaseTransport`.
``websocket`` → :class:`~cliptunnel_mcp.ws_transport.WebSocketTransport`.

Encryption is handled at the protocol level (:func:`~cliptunnel_mcp.protocol.pack`
and :func:`~cliptunnel_mcp.protocol.unpack`) when ``CLIPTUNNEL_AES_KEY`` is set,
not at the transport level.

All imports are lazy (inside the function body) so importing this module
never pulls in ``clipboard-event`` or ``cryptography`` at import time.
"""
from __future__ import annotations

from cliptunnel_mcp import config
from cliptunnel_mcp.transport import Transport

__all__ = ["build_transport"]


def _ensure_loaded() -> None:
    """Ensure register_builtins has run on the module-level registry.

    If the registry already has transports registered (e.g. from a prior
    test that called register_builtins directly), just mark as loaded.
    """
    from cliptunnel_mcp import plugins
    if not plugins._loaded:
        if not plugins.registry.transport_names():
            plugins.register_builtins(plugins.registry)
        plugins._loaded = True


def build_transport() -> Transport:
    """Build the transport selected by ``CLIPTUNNEL_TRANSPORT`` (or its
    ``[transport] type`` config-file equivalent).

    Returns the raw transport; encryption is handled at the protocol level.

    Raises :class:`ValueError` for unknown transport selectors or missing
    required settings.
    """
    _ensure_loaded()
    from cliptunnel_mcp.plugins import registry

    choice = config.get_env("CLIPTUNNEL_TRANSPORT", "clipboard").strip().lower()

    try:
        factory = registry.get_transport_factory(choice)
    except KeyError:
        available = ", ".join(sorted(registry.transport_names()))
        raise ValueError(
            f"CLIPTUNNEL_TRANSPORT='{choice}' is not supported. "
            f"Available transports: {available}"
        ) from None

    config_dict = {
        "CLIPTUNNEL_TRANSPORT": choice,
    }
    return factory(config_dict)