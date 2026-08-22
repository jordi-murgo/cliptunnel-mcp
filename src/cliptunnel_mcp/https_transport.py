"""HTTPS repeater transport — Transport + RevisionMonitor over SSE+POST.

This module implements :class:`~cliptunnel_mcp.transport.Transport` and
:class:`~cliptunnel_mcp.transport.RevisionMonitor` against a remote
repeater service using HTTP POST for writes and Server-Sent Events (SSE)
for real-time cache updates.

The transport is stdlib-only (``urllib.request``) for the production path.
Tests inject a :class:`~tests.fake_repeater.FakeRepeater` via the
``http_client`` parameter to avoid any network I/O.

When ``aes_key`` is set, every payload is encrypted with AES-256-GCM via
:mod:`cliptunnel_mcp.crypto` before transmission and decrypted on receipt.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterator, Protocol, runtime_checkable

from cliptunnel_mcp import crypto

__all__ = [
    "HttpClient",
    "HttpResponse",
    "StreamResponse",
    "TransportError",
    "TransportAuthError",
    "HttpsTransport",
]

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HttpClient protocol and response types
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    """Minimal HTTP response — status code and body string."""

    status: int
    body: str


@runtime_checkable
class StreamResponse(Protocol):
    """Iterable of SSE line strings with a ``close()`` method."""

    def __iter__(self) -> Iterator[str]: ...

    def close(self) -> None: ...


@runtime_checkable
class HttpClient(Protocol):
    """Minimal HTTP client interface used by :class:`HttpsTransport`."""

    def post(
        self, url: str, headers: dict[str, str], body: str, timeout: float
    ) -> HttpResponse: ...

    def get(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse: ...

    def open_stream(
        self, url: str, headers: dict[str, str]
    ) -> StreamResponse: ...


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TransportError(Exception):
    """Base exception for HttpsTransport failures."""


class TransportAuthError(TransportError):
    """Bearer token rejected by the repeater (HTTP 401)."""


# ---------------------------------------------------------------------------
# Production HttpClient (urllib)
# ---------------------------------------------------------------------------


class _UrllibHttpClient:
    """Production :class:`HttpClient` wrapping :mod:`urllib.request`.

    ``open_stream`` returns the raw HTTP response object as the iterable
    (``http.client.HTTPResponse`` supports iteration line by line) with
    ``close()`` delegating to the response.
    """

    def post(
        self, url: str, headers: dict[str, str], body: str, timeout: float
    ) -> HttpResponse:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                body_text = resp.read().decode("utf-8")
            return HttpResponse(status=status, body=body_text)
        except urllib.error.HTTPError as exc:
            return HttpResponse(status=exc.code, body=exc.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"POST {url} failed: {exc}") from exc

    def get(
        self, url: str, headers: dict[str, str], timeout: float
    ) -> HttpResponse:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                body_text = resp.read().decode("utf-8")
            return HttpResponse(status=status, body=body_text)
        except urllib.error.HTTPError as exc:
            return HttpResponse(status=exc.code, body=exc.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"GET {url} failed: {exc}") from exc

    def open_stream(self, url: str, headers: dict[str, str]) -> StreamResponse:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            resp = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TransportAuthError(
                    f"GET {url} returned 401: {exc.read().decode('utf-8')}"
                ) from exc
            raise TransportError(f"GET {url} failed: {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"GET {url} stream failed: {exc}") from exc
        return resp  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HttpsTransport
# ---------------------------------------------------------------------------


class HttpsTransport:
    """Transport + RevisionMonitor backed by an HTTPS repeater.

    Parameters
    ----------
    repeater_url:
        Base URL of the repeater (e.g. ``https://relay.example.com``).
        No trailing slash.
    bearer_token:
        Bearer token sent as ``Authorization: Bearer <token>``.
    aes_key:
        32-byte AES-256 key.  When ``None``, plaintext passthrough.
    poll_timeout:
        Seconds before ``wait_for_change`` returns on no change.
    sse_reconnect_delay:
        Seconds to wait before reconnecting SSE after a drop.
    request_timeout:
        HTTP request timeout for POST and GET (non-SSE) calls.
    http_client:
        Optional :class:`HttpClient` for dependency injection.
        When ``None``, uses the built-in :class:`_UrllibHttpClient`.
    """

    def __init__(
        self,
        *,
        repeater_url: str,
        bearer_token: str,
        aes_key: bytes | None = None,
        poll_timeout: float = 30.0,
        sse_reconnect_delay: float = 1.0,
        request_timeout: float = 15.0,
        http_client: HttpClient | None = None,
    ) -> None:
        self._repeater_url = repeater_url.rstrip("/")
        self._bearer_token = bearer_token
        self._aes_key = aes_key
        self._poll_timeout = poll_timeout
        self._sse_reconnect_delay = sse_reconnect_delay
        self._request_timeout = request_timeout
        self._http_client: HttpClient = http_client or _UrllibHttpClient()

        self._condition = threading.Condition()
        self._value: str = ""
        self._revision: int = 0


        self._running = True
        self._sse_response: StreamResponse | None = None
        self._sse_thread = threading.Thread(
            target=self._sse_loop, daemon=True, name="HttpsTransport-SSE"
        )
        self._sse_thread.start()

    # ------------------------------------------------------------------
    # Transport protocol
    # ------------------------------------------------------------------

    def read(self) -> str:
        """Return the locally cached slot value. Never blocks."""
        with self._condition:
            return self._value

    def write(self, value: str) -> None:
        """POST the value to the repeater and update local state optimistically.

        Raises :class:`TransportAuthError` on HTTP 401 and
        :class:`TransportError` on other failures.
        """
        payload = value
        if self._aes_key is not None:
            try:
                payload = crypto.encrypt(value, self._aes_key)
            except (ValueError, ImportError) as exc:
                raise TransportError(f"encryption failed: {exc}") from exc

        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Content-Type": "text/plain",
        }
        url = f"{self._repeater_url}/slot"
        resp = self._http_client.post(
            url, headers, payload, self._request_timeout
        )

        if resp.status == 401:
            raise TransportAuthError(
                f"POST {url} returned 401: {resp.body}"
            )
        if not (200 <= resp.status < 300):
            raise TransportError(
                f"POST {url} returned {resp.status}: {resp.body}"
            )

        # Parse revision from response.
        try:
            data = json.loads(resp.body)
            remote_rev = data.get("revision", 0)
        except (json.JSONDecodeError, ValueError):
            remote_rev = self._revision + 1

        with self._condition:
            self._value = value  # optimistic: store plaintext
            self._revision = max(self._revision + 1, remote_rev)
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # RevisionMonitor protocol
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        with self._condition:
            return self._revision

    def wait_for_change(self, after: int, timeout: float = 1.0) -> int:
        """Block until ``revision > after`` or timeout, then return revision."""
        with self._condition:
            self._condition.wait_for(
                lambda: self._revision > after, timeout=timeout
            )
            return self._revision

    # ------------------------------------------------------------------
    # SSE background thread
    # ------------------------------------------------------------------

    def _sse_loop(self) -> None:
        """Daemon thread: connect to SSE, parse events, reconnect on drop."""
        while self._running:
            try:
                self._connect_and_read_sse()
            except TransportAuthError:
                _log.error("SSE auth failure (401) — stopping SSE thread")
                return
            except Exception as exc:
                _log.warning("SSE error: %s", exc)

            if not self._running:
                break
            # Sleep before reconnecting
            threading.Event().wait(self._sse_reconnect_delay)
            # Resync via snapshot before reconnecting to SSE
            if self._running:
                self._resync_via_snapshot()

    def _connect_and_read_sse(self) -> None:
        """Open the SSE stream and process events until disconnected."""
        url = f"{self._repeater_url}/slot/events"
        headers = {
            "Authorization": f"Bearer {self._bearer_token}",
            "Accept": "text/event-stream",
        }

        try:
            stream = self._http_client.open_stream(url, headers)
        except TransportAuthError:
            raise
        except Exception as exc:
            raise TransportError(f"open_stream {url} failed: {exc}") from exc

        self._sse_response = stream

        event_type = ""
        data_lines: list[str] = []

        for line in stream:
            if not self._running:
                break
            line = line.rstrip("\n").rstrip("\r")
            if line.startswith(":"):
                # comment / keepalive — no-op
                continue
            if line == "":
                # dispatch accumulated event
                if event_type == "write" and data_lines:
                    try:
                        payload = json.loads("\n".join(data_lines))
                        self._handle_sse_write(payload)
                    except (json.JSONDecodeError, ValueError) as exc:
                        _log.warning("SSE: failed to parse event data: %s", exc)
                event_type = ""
                data_lines = []
                continue
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())

        # Stream ended (disconnected)
        self._sse_response = None

    def _handle_sse_write(self, payload: dict[str, object]) -> None:
        """Process an SSE ``write`` event: decrypt, update cache, bump revision."""
        event_revision = payload.get("revision", 0)
        raw_value = payload.get("value", "")

        if self._aes_key is not None:
            try:
                value = crypto.decrypt(raw_value, self._aes_key)
            except (ValueError, ImportError) as exc:
                _log.warning("SSE: decryption failed, skipping event: %s", exc)
                return
        else:
            value = raw_value

        with self._condition:
            self._value = value
            self._revision = max(self._revision, event_revision)
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Snapshot resync (called on reconnect)
    # ------------------------------------------------------------------

    def _resync_via_snapshot(self) -> None:
        """GET /slot snapshot and update local cache + revision."""
        url = f"{self._repeater_url}/slot"
        headers = {"Authorization": f"Bearer {self._bearer_token}"}
        try:
            resp = self._http_client.get(
                url, headers, self._request_timeout
            )
        except TransportError as exc:
            _log.warning("snapshot GET failed: %s", exc)
            return

        if resp.status != 200:
            _log.warning("snapshot GET returned %d", resp.status)
            return

        try:
            data = json.loads(resp.body)
            snapshot_revision = data.get("revision", 0)
            snapshot_value = data.get("value", "")
        except (json.JSONDecodeError, ValueError) as exc:
            _log.warning("snapshot parse failed: %s", exc)
            return

        if self._aes_key is not None:
            try:
                snapshot_value = crypto.decrypt(snapshot_value, self._aes_key)
            except (ValueError, ImportError) as exc:
                _log.warning("snapshot decryption failed: %s", exc)
                return

        with self._condition:
            self._value = snapshot_value
            self._revision = max(self._revision, snapshot_revision)
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        return "https"

    def close(self) -> None:
        """Stop the SSE thread and close any open stream. Idempotent."""
        self._running = False
        with self._condition:
            self._condition.notify_all()
        # Close the SSE stream so the iterator unblocks
        if self._sse_response is not None:
            try:
                self._sse_response.close()
            except Exception:
                pass
            self._sse_response = None
        # Join with a short timeout (daemon thread)
        if self._sse_thread.is_alive():
            self._sse_thread.join(timeout=2.0)