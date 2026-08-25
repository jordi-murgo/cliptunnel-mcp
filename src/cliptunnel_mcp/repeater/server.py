"""Repeater HTTP server — stdlib ThreadingHTTPServer + SSE.

Three endpoints:
  POST /slot         — write slot value (auth required)
  GET  /slot         — snapshot: returns current value + revision
  GET  /slot/events  — SSE stream: pushes ``write`` events to subscribers

Bearer token auth via :meth:`RepeaterState.validate_token`.

Stdlib only — :mod:`http.server`, :mod:`threading`, :mod:`queue`, :mod:`json`.
"""
from __future__ import annotations

import json
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cliptunnel_mcp.repeater.state import RepeaterState

__all__ = ["make_handler", "RepeaterServer"]

# SSE keepalive interval (seconds).
_KEEPALIVE_INTERVAL = 15.0


def make_handler(state: RepeaterState) -> type[BaseHTTPRequestHandler]:
    """Return a :class:`BaseHTTPRequestHandler` subclass closing over *state*."""

    class RepeaterHandler(BaseHTTPRequestHandler):
        # Suppress per-request stderr noise.
        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            pass

        # ── Auth ──────────────────────────────────────────────────

        def _extract_token(self) -> str | None:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[len("Bearer "):].strip()
            return None

        def _check_auth(self) -> bool:
            token = self._extract_token()
            if token is None:
                return False
            return state.validate_token(token)

        def _send_unauthorized(self) -> None:
            body = json.dumps({"error": "unauthorized"})
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        # ── POST /slot ───────────────────────────────────────────

        def do_POST(self) -> None:  # noqa: N802
            if not self._check_auth():
                # Drain the request body so the socket buffer is empty before
                # we send the 401.  Without this, Windows aborts the connection
                # (WinError 10053) when the handler returns with unread data,
                # and the client sees ConnectionAbortedError instead of 401.
                length = int(self.headers.get("Content-Length", 0))
                if length > 0:
                    self.rfile.read(length)
                self._send_unauthorized()
                return

            if self.path != "/slot":
                self.send_error(404)
                return

            length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(length) if length > 0 else b""
            value = body_bytes.decode("utf-8")
            rev = state.write(value)
            resp = json.dumps({"revision": rev})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp.encode("utf-8"))

        # ── GET /slot, GET /slot/events ─────────────────────────

        def do_GET(self) -> None:  # noqa: N802
            if not self._check_auth():
                self._send_unauthorized()
                return

            if self.path == "/slot":
                self._handle_snapshot()
            elif self.path == "/slot/events":
                self._handle_sse()
            else:
                self.send_error(404)

        def _handle_snapshot(self) -> None:
            val, rev = state.snapshot()
            resp = json.dumps({"revision": rev, "value": val})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp.encode("utf-8"))

        def _handle_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            q = state.add_subscriber()
            try:
                while True:
                    try:
                        event_data = q.get(timeout=_KEEPALIVE_INTERVAL)
                    except queue.Empty:
                        # Send keepalive comment.
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(event_data.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                state.remove_subscriber(q)

    return RepeaterHandler


class RepeaterServer(ThreadingHTTPServer):
    """Thin :class:`ThreadingHTTPServer` wrapper for the repeater."""

    allow_reuse_address = True
    daemon_threads = True