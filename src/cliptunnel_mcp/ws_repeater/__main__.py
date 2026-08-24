"""WebSocket repeater server entry point.

Run with: ``python -m cliptunnel_mcp.ws_repeater [options]``

Environment variables:
  REPEATER_TOKENS   Comma-separated ``name:token`` pairs (required).
  REPEATER_PORT     Port to listen on (default 9000).
  REPEATER_HOST     Host to bind (default 0.0.0.0).
  REPEATER_TLS_CERT Path to TLS certificate file (optional).
  REPEATER_TLS_KEY  Path to TLS key file (optional).
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import ssl
import sys

from cliptunnel_mcp.ws_repeater.server import handler_factory
from cliptunnel_mcp.ws_repeater.state import WsRepeaterState

_log = logging.getLogger(__name__)


def _parse_tokens(raw: str | None) -> dict[str, str] | None:
    """Parse ``name:token`` pairs from a comma-separated string.

    Returns a dict mapping token → name (reversed for lookup by token value).
    Returns ``None`` when *raw* is ``None`` (accept all).
    """
    if raw is None:
        return None
    token_to_name: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            name, token = pair.split(":", 1)
            token_to_name[token.strip()] = name.strip()
        else:
            token_to_name[pair.strip()] = "unnamed"
    return token_to_name


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    tokens = _parse_tokens(os.environ.get("REPEATER_TOKENS"))
    if not tokens:
        print("ERROR: REPEATER_TOKENS is required (format: name:token,...)",
              file=sys.stderr)
        sys.exit(1)

    port = int(os.environ.get("REPEATER_PORT", "9000"))
    host = os.environ.get("REPEATER_HOST", "0.0.0.0")

    tls_cert = os.environ.get("REPEATER_TLS_CERT")
    tls_key = os.environ.get("REPEATER_TLS_KEY")
    ssl_context: ssl.SSLContext | None = None
    if tls_cert and tls_key:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(tls_cert, tls_key)

    state = WsRepeaterState(tokens=tokens)
    handler = handler_factory(state)

    async def run() -> None:
        import websockets

        async with websockets.serve(handler, host, port, ssl=ssl_context, ping_interval=None, ping_timeout=None):
            _log.info("WS repeater listening on %s:%d", host, port)
            # Wait for SIGINT/SIGTERM
            stop = asyncio.Future()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set_result, None)
            await stop

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()