"""terrarium — Python client for the terrarium orchestrator API.

The surface mirrors the Claude Agent SDK (``query``, ``TerrariumOptions``, typed messages,
``can_use_tool`` + ``PermissionResultAllow/Deny``) with extra parameters for Terrarium's own
features (personas, approval gating, egress profiles, shared memory, live model/permission
switching). Like the Claude Agent SDK it is **async-only** — it talks to a running
orchestrator over HTTP/SSE (the Claude SDK spawns a CLI instead).

    import asyncio

    # one-shot, Claude-Agent-SDK style
    from terrarium import query, TerrariumOptions

    async def main():
        async for msg in query(prompt="What is 12 * 9?", options=TerrariumOptions(model="sonnet")):
            print(msg)

    asyncio.run(main())

    # full client (agents, sessions, schedules, egress profiles, tokens)
    from terrarium import TerrariumClient, TerrariumOptions

    async def research():
        async with TerrariumClient("http://127.0.0.1:8900") as client:
            agent = await client.agents.create(name="Researcher", model="sonnet", system_mode="assistant")
            async with client.session(options=TerrariumOptions(agent_id=agent["id"])) as s:
                async for msg in s.receive_response("Summarise the repo"):
                    print(msg)

    asyncio.run(research())
"""

# Single source of truth is pyproject.toml; read it back from the installed distribution so the
# User-Agent can never drift from the published version. Falls back for an uninstalled checkout.
#
# The lookup key is the DISTRIBUTION name (`terrarium-python`), not this import package
# (`terrarium`). They differ, and using the import name here silently degrades every install to
# "0+unknown" — which is not an error anywhere, just a wrong User-Agent forever.
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("terrarium-python")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"
del _pkg_version, PackageNotFoundError

from .client import (
    AgentsResource, EgressProfilesResource, EnvironmentsResource, SchedulesResource,
    SecretsResource, Session, SessionsResource, TerrariumClient, TokensResource, query,
)
from .memory import MemoryStore, SqliteMemory, memory_tools
from .options import (
    AgentDefinition, CanUseTool, ClientTool, PermissionResultAllow, PermissionResultDeny,
    TerrariumOptions, ToolPermissionContext, tool,
)
from .messages import (
    AssistantMessage, ContentBlock, Message, PermissionMessage, QuestionMessage,
    ResultMessage, SystemMessage, TextBlock, ThinkingBlock, ToolResultBlock,
    ToolUseBlock, UserMessage, parse_message,
)
from .errors import (
    AuthError, ConflictError, NotFoundError, RateLimitError, ServerError,
    TerrariumError, TransportError,
)

__all__ = [
    # client + one-shot
    "TerrariumClient", "Session", "query",
    "AgentsResource", "SessionsResource", "SchedulesResource",
    "EgressProfilesResource", "EnvironmentsResource", "SecretsResource", "TokensResource",
    # options + permissions (Claude-SDK aligned)
    "TerrariumOptions", "AgentDefinition", "CanUseTool", "ToolPermissionContext",
    "PermissionResultAllow", "PermissionResultDeny",
    # client-side custom tools (run in the dev's process, bridged to the sandbox)
    "ClientTool", "tool",
    # structured, retrieval-backed memory for long-running agents (terrarium.memory)
    "MemoryStore", "SqliteMemory", "memory_tools",
    # typed messages
    "Message", "AssistantMessage", "UserMessage", "SystemMessage", "ResultMessage",
    "QuestionMessage", "PermissionMessage", "ContentBlock",
    "TextBlock", "ThinkingBlock", "ToolUseBlock", "ToolResultBlock", "parse_message",
    # errors
    "TerrariumError", "AuthError", "NotFoundError", "ConflictError",
    "RateLimitError", "ServerError", "TransportError",
    "__version__",
]
