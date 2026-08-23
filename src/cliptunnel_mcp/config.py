"""TOML configuration-file layer.

Settings can live either in environment variables or in a hidden,
user-only-readable TOML file. Precedence everywhere:

    environment variable  >  config file  >  built-in default

Default file path is ``~/.cliptunnel/config.toml``; it may be overridden
per process via ``CLIPTUNNEL_CONFIG`` env var, or by the CLI
``--config PATH`` flag (:func:`set_config_path`). Path precedence for a
single resolution: explicit argument > ``set_config_path`` override >
``CLIPTUNNEL_CONFIG`` env var > default.

File layout::

    [transport]
    type = "clipboard"            # or "https"
    repeater_url = "https://..."
    repeater_token = "..."

    [encryption]
    aes_key = "<base64 32 bytes>"

    [heartbeat]
    interval_secs = 120

    [copilot]
    oauth_token = "gho_..."       # replaces legacy .copilot_agent_token lookup

A missing file is not an error (empty settings). Malformed TOML raises
:class:`ValueError`. If the file's permissions allow group/other read,
a :func:`logging.warning` recommends ``chmod 600`` (never fatal).
"""
from __future__ import annotations

import logging
import os

try:  # stdlib on Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "ENV_TO_FILE",
    "get_copilot_token",
    "get_env",
    "load_config",
    "set_config_path",
]

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".cliptunnel", "config.toml")

# Environment variable name -> ([section], key) in the TOML document.
ENV_TO_FILE: dict[str, tuple[tuple[str, ...], str]] = {
    "CLIPTUNNEL_TRANSPORT": (("transport",), "type"),
    "CLIPTUNNEL_REPEATER_URL": (("transport",), "repeater_url"),
    "CLIPTUNNEL_REPEATER_TOKEN": (("transport",), "repeater_token"),
    "CLIPTUNNEL_AES_KEY": (("encryption",), "aes_key"),
    "CLIPTUNNEL_HEARTBEAT_SECS": (("heartbeat",), "interval_secs"),
}

# Explicit --config override set by the CLI entry points. Beats CLIPTUNNEL_CONFIG.
_override_path: str | None = None

# Cache keyed by (absolute path, mtime_ns, size): rewriting the file yields a
# fresh key, so stale entries are never served and no invalidation is needed.
_cache: dict[tuple[str, int | None, int | None], dict] = {}


def _resolve_path(path: str | None) -> str:
    """Resolve the effective config path.

    Precedence: explicit argument > set_config_path override >
    ``CLIPTUNNEL_CONFIG`` env var > default path.
    """
    candidate = (
        path
        or _override_path
        or os.environ.get("CLIPTUNNEL_CONFIG")
        or DEFAULT_CONFIG_PATH
    )
    return os.path.abspath(os.path.expanduser(candidate))


def load_config(path: str | None = None, *, force_reload: bool = False) -> dict:
    """Load and parse the TOML config file.

    Returns an empty dict when the file does not exist. Raises
    :class:`ValueError` with the offending path when the file contains
    malformed TOML.
    """
    resolved = _resolve_path(path)
    try:
        st = os.stat(resolved)
    except FileNotFoundError:
        return {}
    except OSError as exc:  # pragma: no cover - unusual filesystem errors
        raise ValueError(f"Cannot stat config file {resolved}: {exc}") from exc

    key = (resolved, st.st_mtime_ns, st.st_size)
    if not force_reload and key in _cache:
        return _cache[key]

    try:
        with open(resolved, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in config file {resolved}: {exc}") from exc

    _warn_if_group_readable(resolved)

    if len(_cache) > 16:  # keep the cache bounded across many temp files
        _cache.clear()
    _cache[key] = data
    return data


def _warn_if_group_readable(path: str) -> None:
    """Warn when a POSIX config file is readable by group/others.

    Skipped on Windows: ``st_mode`` there does not reflect POSIX permission
    bits (files always report ``0o666`` regardless of ACLs), so the check
    would warn on every file. Windows enforces access via ACLs instead.
    """
    if os.name != "posix":
        return
    mode = os.stat(path).st_mode
    if mode & 0o077:
        logger.warning(
            "Config file %s is readable by group/others and contains secrets; "
            "run: chmod 600 %s",
            path,
            path,
        )


def get_env(
    name: str,
    default: str | None = None,
    *,
    config_path: str | None = None,
) -> str | None:
    """:meth:`os.environ.get` drop-in that consults the config file layer.

    The environment variable wins when set to a non-empty value; otherwise
    the mapped config-file key is consulted; otherwise *default* is returned.
    """
    value = os.environ.get(name)
    if value is not None and value != "":
        return value

    mapping = ENV_TO_FILE.get(name)
    if mapping is None:
        return default

    sections, key = mapping
    node: object = load_config(config_path)
    for section in sections:
        if not isinstance(node, dict):
            return default
        node = node.get(section)
    if isinstance(node, dict):
        raw = node.get(key)
    else:
        raw = node
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if raw is not None and not isinstance(raw, str):
        # Non-string scalar (e.g. heartbeat interval_secs = 30).
        return str(raw)
    return default


def get_copilot_token(*, config_path: str | None = None) -> str | None:
    """Return ``[copilot].oauth_token`` from the config file, if set."""
    token = load_config(config_path).get("copilot", {}).get("oauth_token")
    if isinstance(token, str):
        token = token.strip()
        if token:
            return token
    return None


def set_config_path(path: str | None) -> None:
    """Override the config path used when no explicit argument is given.

    Called early by the CLI entry points for their ``--config PATH`` flag.
    Pass ``None`` to clear the override (used by tests).
    """
    global _override_path
    _override_path = path
