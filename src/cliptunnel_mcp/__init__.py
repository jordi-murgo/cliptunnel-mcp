"""cliptunnel-mcp — operate a locked-down remote machine through its clipboard."""
from cliptunnel_mcp.controller import Controller
from cliptunnel_mcp.protocol import (
    PROTOCOL_SIG,
    PROTOCOL_VERSION,
    Message,
    MsgType,
    Role,
    SeqTracker,
    pack,
    unpack,
    validate,
)
from cliptunnel_mcp.operations import dispatch

__version__ = "0.3.2"

__all__ = [
    "PROTOCOL_SIG",
    "PROTOCOL_VERSION",
    "Controller",
    "Message",
    "MsgType",
    "Role",
    "SeqTracker",
    "dispatch",
    "pack",
    "validate",
]


def __getattr__(name: str):
    """Lazy-import Agent to avoid runpy RuntimeWarning with python -m."""
    if name == "Agent":
        from cliptunnel_mcp.agent import Agent

        return Agent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

