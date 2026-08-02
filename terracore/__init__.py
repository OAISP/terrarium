"""terracore — shared library for the sandboxed Claude agent.

Pure-Python building blocks used by both the host orchestrator and the
in-container worker. The worker additionally imports ``terracore.tools`` (which
pulls in claude-agent-sdk); the host does not need the SDK, so keep this module
free of SDK imports.
"""

from .events import EventStore, now_iso

__all__ = ["EventStore", "now_iso"]
