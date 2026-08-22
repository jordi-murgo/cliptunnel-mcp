"""Repeater entry point — ``python -m cliptunnel_mcp.repeater``.

Reads environment variables:
  REPEATER_TOKENS   — comma-separated ``name:token`` pairs (required).
  REPEATER_PORT     — port to listen on (default 8443).
  REPEATER_HOST     — bind address (default 0.0.0.0).
"""
from __future__ import annotations

import logging
import os
import sys

from cliptunnel_mcp.repeater.server import RepeaterServer, make_handler
from cliptunnel_mcp.repeater.state import RepeaterState

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8443
_DEFAULT_HOST = "0.0.0.0"


def _parse_tokens(raw: str) -> set[str]:
    """Parse ``REPEATER_TOKENS`` into a set of token values.

    Format: ``name:token,name:token,...``.  The ``name`` is for logging only;
    auth checks only the token value.
    """
    tokens: set[str] = set()
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            _, token = pair.split(":", 1)
        else:
            token = pair
        token = token.strip()
        if token:
            tokens.add(token)
    return tokens


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    raw_tokens = os.environ.get("REPEATER_TOKENS", "")
    if not raw_tokens.strip():
        logger.error("REPEATER_TOKENS is required (comma-separated name:token pairs)")
        sys.exit(1)

    tokens = _parse_tokens(raw_tokens)
    if not tokens:
        logger.error("REPEATER_TOKENS parsed to zero valid tokens — refusing to start")
        sys.exit(1)

    port = int(os.environ.get("REPEATER_PORT", _DEFAULT_PORT))
    host = os.environ.get("REPEATER_HOST", _DEFAULT_HOST)

    state = RepeaterState(tokens=tokens)
    handler = make_handler(state)
    srv = RepeaterServer((host, port), handler)

    logger.info("repeater listening on %s:%d (behind TLS proxy)", host, port)
    logger.info("configured %d token(s)", len(tokens))

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()