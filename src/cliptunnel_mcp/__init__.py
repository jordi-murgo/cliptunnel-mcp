"""cliptunnel-mcp — operate a locked-down remote machine through its clipboard."""
from cliptunnel_mcp.agent import Agent
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

__version__ = "0.1.0"

__all__ = [
    "PROTOCOL_SIG",
    "PROTOCOL_VERSION",
    "Agent",
    "Controller",
    "Message",
    "MsgType",
    "RevisionMonitor",
    "SeqTracker",
    "Transport",
    "dispatch",
    "pack",
    "validate",
]
