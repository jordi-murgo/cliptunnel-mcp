"""cliptunnel-mcp — operate a locked-down remote machine through its clipboard."""
from cliptunnel_mcp.controller import Controller
from cliptunnel_mcp.protocol import (
    BROADCAST_ADDR,
    CONTROLLER_ADDR,
    PROTOCOL_SIG,
    PROTOCOL_VERSION,
    Message,
    MsgType,
    SeqTracker,
    generate_controller_id,
    generate_remote_id,
    is_broadcast,
    is_controller,
    is_remote,
    is_valid_from_address,
    is_valid_to_address,
    pack,
    unpack,
    validate,
)
from cliptunnel_mcp.operations import dispatch

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    __version__ = _pkg_version("cliptunnel-mcp")
except PackageNotFoundError:  # package not installed (e.g. running from source)
    __version__ = "0.0.0.dev0"

__all__ = [
    "BROADCAST_ADDR",
    "CONTROLLER_ADDR",
    "Controller",
    "Message",
    "MsgType",
    "PROTOCOL_SIG",
    "PROTOCOL_VERSION",
    "SeqTracker",
    "dispatch",
    "generate_controller_id",
    "generate_remote_id",
    "is_broadcast",
    "is_controller",
    "is_remote",
    "is_valid_from_address",
    "is_valid_to_address",
    "pack",
    "unpack",
    "validate",
]


def __getattr__(name: str):
    """Lazy-import Agent to avoid runpy RuntimeWarning with python -m."""
    if name == "Agent":
        from cliptunnel_mcp.agent import Agent

        return Agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")