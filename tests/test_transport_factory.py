"""Tests for build_transport() factory — T4 (RED first).

Reads CLIPTUNNEL_TRANSPORT (default clipboard, case-insensitive).
clipboard → ClipboardTransport (lazy import).
https → HttpsTransport (lazy import, validates URL + TOKEN, parses AES_KEY).
Unknown → ValueError.
"""
from __future__ import annotations

import base64

import pytest

from cliptunnel_mcp.clipboard_transport import ClipboardTransport
from cliptunnel_mcp.https_transport import HttpsTransport
from cliptunnel_mcp.encrypted_transport import EncryptedTransport
from cliptunnel_mcp.transport_factory import build_transport

class TestDefaults:
    def test_no_env_returns_clipboard(self, monkeypatch):
        monkeypatch.delenv("CLIPTUNNEL_TRANSPORT", raising=False)
        t = build_transport()
        assert isinstance(t, ClipboardTransport)
        t.close()

    def test_explicit_clipboard(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "clipboard")
        t = build_transport()
        assert isinstance(t, ClipboardTransport)
        t.close()


class TestHttps:
    def test_https_returns_https_transport(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "https")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_TOKEN", "secret")
        t = build_transport()
        assert isinstance(t, HttpsTransport)
        t.close()

    def test_https_missing_url_raises(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "https")
        monkeypatch.delenv("CLIPTUNNEL_REPEATER_URL", raising=False)
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_TOKEN", "secret")
        with pytest.raises(ValueError, match="CLIPTUNNEL_REPEATER_URL"):
            build_transport()

    def test_https_missing_token_raises(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "https")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
        monkeypatch.delenv("CLIPTUNNEL_REPEATER_TOKEN", raising=False)
        with pytest.raises(ValueError, match="CLIPTUNNEL_REPEATER_TOKEN"):
            build_transport()

    def test_https_missing_both_raises(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "https")
        monkeypatch.delenv("CLIPTUNNEL_REPEATER_URL", raising=False)
        monkeypatch.delenv("CLIPTUNNEL_REPEATER_TOKEN", raising=False)
        with pytest.raises(ValueError, match="CLIPTUNNEL_REPEATER_URL"):
            build_transport()

    def test_https_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "HTTPS")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_TOKEN", "secret")
        t = build_transport()
        assert isinstance(t, HttpsTransport)
        t.close()


class TestAESKey:
    def test_valid_aes_key(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "https")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_TOKEN", "secret")
        monkeypatch.setenv("CLIPTUNNEL_AES_KEY", base64.b64encode(b"0" * 32).decode())
        t = build_transport()
        assert isinstance(t, EncryptedTransport)
        t.close()

    def test_wrong_length_aes_key_raises(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "https")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_TOKEN", "secret")
        monkeypatch.setenv("CLIPTUNNEL_AES_KEY", base64.b64encode(b"0" * 16).decode())
        with pytest.raises(ValueError, match="32 bytes"):
            build_transport()

    def test_bad_base64_aes_key_raises(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "https")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
        monkeypatch.setenv("CLIPTUNNEL_REPEATER_TOKEN", "secret")
        monkeypatch.setenv("CLIPTUNNEL_AES_KEY", "!!!not-base64!!!")
        with pytest.raises(ValueError):
            build_transport()


class TestUnknown:
    def test_unknown_transport_raises(self, monkeypatch):
        monkeypatch.setenv("CLIPTUNNEL_TRANSPORT", "carrier-pigeon")
        with pytest.raises(ValueError, match="not supported"):
            build_transport()