# tests/test_fake_ws_repeater.py
from __future__ import annotations

import json
import unittest

from cliptunnel_mcp.https_transport import TransportAuthError
from tests.fake_ws_repeater import FakeWsRepeater, FakeWsConnection


class TestFakeWsRepeaterConnect(unittest.TestCase):
    def test_connect_valid_token_returns_connection(self) -> None:
        r = FakeWsRepeater(tokens=["test-token"])
        conn = r.connect("test-token")
        self.assertIsInstance(conn, FakeWsConnection)
        self.assertTrue(conn.authenticated)

    def test_connect_sends_initial_snapshot(self) -> None:
        r = FakeWsRepeater()
        conn = r.connect("t")
        # The first recv should be the snapshot.
        msg = json.loads(conn.recv_nowait())
        self.assertEqual(msg["type"], "snapshot")
        self.assertEqual(msg["value"], "")
        self.assertEqual(msg["revision"], 0)

    def test_connect_invalid_token_raises_auth_error(self) -> None:
        r = FakeWsRepeater(tokens=["good"])
        with self.assertRaises(TransportAuthError):
            r.connect("bad")

    def test_connect_no_tokens_accepts_all(self) -> None:
        r = FakeWsRepeater(tokens=None)
        conn = r.connect("anything")
        self.assertTrue(conn.authenticated)


class TestFakeWsRepeaterWrite(unittest.TestCase):
    def test_send_write_stores_value_and_returns_write_ack(self) -> None:
        r = FakeWsRepeater()
        conn = r.connect("t")
        # Consume initial snapshot
        conn.recv_nowait()
        resp = json.loads(conn.send(json.dumps({"type": "write", "value": "hello"})))
        self.assertEqual(resp["type"], "write_ack")
        self.assertEqual(resp["revision"], 1)
        self.assertEqual(r.value, "hello")
        self.assertEqual(r.revision, 1)

    def test_writes_list_records_all_values(self) -> None:
        r = FakeWsRepeater()
        conn = r.connect("t")
        conn.recv_nowait()  # snapshot
        conn.send(json.dumps({"type": "write", "value": "a"}))
        conn.send(json.dumps({"type": "write", "value": "b"}))
        self.assertEqual(r.writes, ["a", "b"])

    def test_send_write_pushes_event_to_other_connections(self) -> None:
        r = FakeWsRepeater()
        conn_a = r.connect("t")
        conn_a.recv_nowait()  # snapshot
        conn_b = r.connect("t")
        conn_b.recv_nowait()  # snapshot
        # conn_a writes
        conn_a.send(json.dumps({"type": "write", "value": "from-a"}))
        # conn_b should have an event in its inbox
        event = json.loads(conn_b.recv_nowait())
        self.assertEqual(event["type"], "event")
        self.assertEqual(event["value"], "from-a")
        self.assertEqual(event["revision"], 1)


class TestFakeWsRepeaterPing(unittest.TestCase):
    def test_send_ping_returns_pong(self) -> None:
        r = FakeWsRepeater()
        conn = r.connect("t")
        conn.recv_nowait()  # snapshot
        resp = json.loads(conn.send(json.dumps({"type": "ping"})))
        self.assertEqual(resp["type"], "pong")


class TestFakeWsRepeaterClose(unittest.TestCase):
    def test_close_removes_connection(self) -> None:
        r = FakeWsRepeater()
        conn = r.connect("t")
        conn.close()
        self.assertTrue(conn.closed)
        self.assertNotIn(conn, r.connections)


if __name__ == "__main__":
    unittest.main()