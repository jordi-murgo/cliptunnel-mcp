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
    generate_remote_id,
    pack,
    unpack,
    validate,
)
from cliptunnel_mcp.operations import dispatch

__version__ = "0.5.0"

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
    "generate_remote_id",
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

