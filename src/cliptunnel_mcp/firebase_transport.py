"""Firebase Realtime Database transport — Transport + RevisionMonitor over
the RTDB REST API.

This module implements :class:`~cliptunnel_mcp.transport.Transport` and
:class:`~cliptunnel_mcp.transport.RevisionMonitor` against a Firebase
Realtime Database using its REST API: ``PUT`` replaces the slot node,
``GET`` reads a snapshot, and Server-Sent Events stream node updates in
real time.

The slot is a single JSON node at ``/<node_path>`` shaped
``{"v": "<wire string>", "r": <server timestamp ms>}``. Writes send
``{"v": value, "r": {".sv": "timestamp"}}`` so Firebase fills ``r`` from
its server clock — a monotonic revision shared by all writers, which the
monitor exposes as ``revision``.

Errors and the HTTP client contract are shared with
:mod:`cliptunnel_mcp.https_transport`: auth failures (HTTP 401/403) raise
:class:`~cliptunnel_mcp.https_transport.TransportAuthError`, other
failures raise :class:`~cliptunnel_mcp.https_transport.TransportError`.
The production client extends the shared :class:`_UrllibHttpClient` with
the ``PUT`` verb Firebase requires (RTDB ``POST`` would append a push-id
child instead of replacing the node). Tests inject
:class:`~tests.fake_firebase.FakeFirebase` via ``http_client`` so no
network I/O occurs.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from cliptunnel_mcp.https_transport import (
    HttpClient,
    HttpResponse,
    StreamResponse,
    TransportAuthError,
    TransportError,
    _UrllibHttpClient,
)

__all__ = ["FirebaseTransport"]

_log = logging.getLogger(__name__)


class _FirebaseHttpClient(_UrllibHttpClient):
    """Production :class:`HttpClient` for the RTDB REST API.

    Extends the shared urllib client from
    :mod:`cliptunnel_mcp.https_transport` with the ``PUT`` verb;
    ``get`` and ``open_stream`` (including its 401 →
    :class:`TransportAuthError` mapping and finite 60s socket timeout)
    are inherited unchanged.
    """

    def put(
        self, url: str, headers: dict[str, str], body: str, timeout: float
    ) -> HttpResponse:
        req = urllib.request.Request(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                body_text = resp.read().decode("utf-8")
            return HttpResponse(status=status, body=body_text)
        except urllib.error.HTTPError as exc:
            return HttpResponse(status=exc.code, body=exc.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"PUT {url} failed: {exc}") from exc


class FirebaseTransport:
    """Transport + RevisionMonitor backed by a Firebase Realtime Database.

    Parameters
    ----------
    database_url:
        Base URL of the database (e.g.
        ``https://NAME-default-rtdb.firebaseio.com``). No trailing slash.
    auth_token:
        Firebase auth token, sent both as ``?auth=<token>`` on every
        request URL (the RTDB REST convention) and as
        ``Authorization: Bearer <token>``.
    node_path:
        Path of the slot node inside the database (default ``cliptunnel``).
    sse_reconnect_delay:
        Seconds to wait before reconnecting SSE after a drop.
    request_timeout:
        HTTP request timeout for PUT and GET (non-SSE) calls.
    http_client:
        Optional :class:`HttpClient` for dependency injection. Must also
        provide a ``put`` method (as :class:`_FirebaseHttpClient` and
        :class:`~tests.fake_firebase.FakeFirebase` do). When ``None``,
        uses the built-in :class:`_FirebaseHttpClient`.
    """

    def __init__(
        self,
        *,
        database_url: str,
        auth_token: str,
        node_path: str = "cliptunnel",
        http_client: HttpClient | None = None,
        sse_reconnect_delay: float = 1.0,
        request_timeout: float = 15.0,
    ) -> None:
        self._database_url = database_url.rstrip("/")
        self._auth_token = auth_token
        self._node_path = node_path.strip("/")
        self._sse_reconnect_delay = sse_reconnect_delay
        self._request_timeout = request_timeout
        self._http_client: HttpClient = http_client or _FirebaseHttpClient()

        self._condition = threading.Condition()
        self._value: str = ""
        self._revision: int = 0

        self._running = True
        self._sse_response: StreamResponse | None = None
        self._sse_thread = threading.Thread(
            target=self._sse_loop, daemon=True, name="FirebaseTransport-SSE"
        )
        self._sse_thread.start()

    # ------------------------------------------------------------------
    # URL / headers helpers
    # ------------------------------------------------------------------

    def _node_url(self) -> str:
        """Node URL with the auth token in the query string (RTDB style)."""
        return f"{self._database_url}/{self._node_path}.json?auth={self._auth_token}"

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._auth_token}"}

    # ------------------------------------------------------------------
    # Transport protocol
    # ------------------------------------------------------------------

    def read(self) -> str:
        """Return the locally cached slot value. Never blocks."""
        with self._condition:
            return self._value

    def write(self, value: str) -> None:
        """PUT the node to Firebase and update local state optimistically.

        The body carries ``{"v": value, "r": {".sv": "timestamp"}}`` so
        the server stamps ``r``; the response echoes the stored node and
        its revision is adopted.

        Raises :class:`TransportAuthError` on HTTP 401/403 and
        :class:`TransportError` on other failures.
        """
        headers = {
            **self._auth_headers(),
            "Content-Type": "application/json",
        }
        body = json.dumps({"v": value, "r": {".sv": "timestamp"}})
        url = self._node_url()
        resp = self._http_client.put(url, headers, body, self._request_timeout)  # type: ignore[attr-defined]

        if resp.status in (401, 403):
            raise TransportAuthError(
                f"PUT {url} returned {resp.status}: {resp.body}"
            )
        if not (200 <= resp.status < 300):
            raise TransportError(
                f"PUT {url} returned {resp.status}: {resp.body}"
            )

        # The response is the stored node — adopt its server timestamp.
        try:
            data = json.loads(resp.body)
            remote_rev = int(data.get("r", 0))
        except (json.JSONDecodeError, ValueError, AttributeError, TypeError):
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
                _log.error("SSE auth failure (401/403) — stopping SSE thread")
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
        """Open the SSE stream and process events until disconnected.

        Firebase streams ``event: put`` events whose data is
        ``{"path": "/", "data": {<node>}}``; the first one after opening
        carries the current node state.
        """
        url = self._node_url()
        headers = {
            **self._auth_headers(),
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

        for raw_line in stream:
            if not self._running:
                break
            if isinstance(raw_line, bytes):
                raw_line = raw_line.decode("utf-8", errors="replace")
            line = raw_line.rstrip("\n").rstrip("\r")
            if line.startswith(":"):
                # comment / keepalive — no-op
                continue
            if line == "":
                # dispatch accumulated event
                if event_type == "put" and data_lines:
                    try:
                        payload = json.loads("\n".join(data_lines))
                        self._handle_sse_put(payload)
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

    def _handle_sse_put(self, payload: dict[str, object]) -> None:
        """Process an SSE ``put`` event: update cache and revision.

        ``payload["data"]`` is the node object (``{"v": ..., "r": ...}``);
        each present field is applied and blocked waiters are woken.
        Non-dict payloads (e.g. a ``null`` delete) are ignored.
        """
        node = payload.get("data")
        if not isinstance(node, dict):
            return

        value = node.get("v")
        revision = node.get("r")

        with self._condition:
            if isinstance(value, str):
                self._value = value
            if isinstance(revision, int):
                self._revision = max(self._revision, revision)
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Snapshot resync (called on reconnect)
    # ------------------------------------------------------------------

    def _resync_via_snapshot(self) -> None:
        """GET the node snapshot and update local cache + revision.

        An empty database answers HTTP 200 with the literal body ``null``,
        which leaves the local state untouched.
        """
        url = self._node_url()
        headers = self._auth_headers()
        try:
            resp = self._http_client.get(url, headers, self._request_timeout)
        except TransportError as exc:
            _log.warning("snapshot GET failed: %s", exc)
            return

        if resp.status != 200:
            _log.warning("snapshot GET returned %d", resp.status)
            return

        try:
            data = json.loads(resp.body)
        except (json.JSONDecodeError, ValueError) as exc:
            _log.warning("snapshot parse failed: %s", exc)
            return

        if not isinstance(data, dict):
            return  # empty database ("null") — nothing to sync

        snapshot_value = data.get("v")
        snapshot_revision = data.get("r")

        with self._condition:
            if isinstance(snapshot_value, str):
                self._value = snapshot_value
            if isinstance(snapshot_revision, int):
                self._revision = max(self._revision, snapshot_revision)
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        return "firebase"

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
