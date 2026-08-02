"""The client-tool bridge: an in-process MCP server whose tools don't execute in the
sandbox but are bridged out to the SDK developer's own process (see ``build_client_bridge``).
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool


def _to_mcp_block(b: Any) -> Any:
    """Devs return Anthropic content blocks (the format used everywhere else, incl. user turns).
    An MCP *tool result* uses a different image shape — ``{type:image, data, mimeType}`` instead of
    ``{type:image, source:{type:base64, media_type, data}}`` — so convert image blocks here (the CLI
    re-emits them to the model in Anthropic form). Text + already-MCP blocks pass through."""
    if isinstance(b, dict) and b.get("type") == "image" and isinstance(b.get("source"), dict):
        src = b["source"]
        if src.get("type") == "base64":
            return {"type": "image", "data": src.get("data", ""), "mimeType": src.get("media_type", "image/png")}
    return b


def build_client_bridge(specs, *, emit, pending, next_id):
    """An in-process MCP server whose tools DON'T execute here — each call is bridged to the
    SDK client (the developer's own process) and awaited.

    This is what lets an SDK developer expose **custom tools with their own application
    context** without putting any of that context in the sandbox: only the agent's tool
    INPUT crosses out (as a ``client_tool_call`` event), the dev's handler runs in the dev's
    trusted process, and only the dev-chosen RESULT string crosses back in. The sandbox never
    sees the dev's code, state, or secrets.

    ``specs`` are ``[{name, description, input_schema}]``. Returns ``(server_or_None, names)``
    where names are the agent-visible ``mcp__client__<name>`` ids (so the caller can allow them).
    """
    import asyncio

    from terracore import protocol as P

    sdk_tools, names = [], []
    for spec in specs or []:
        name = str((spec or {}).get("name") or "").strip()
        if not name:
            continue
        desc = str(spec.get("description") or "")
        schema = spec.get("input_schema") if isinstance(spec.get("input_schema"), dict) else {}

        def _make(tool_name: str):
            async def handler(args):
                call_id = next_id("ct")
                fut = asyncio.get_running_loop().create_future()
                pending[call_id] = fut
                emit(P.EV_CLIENT_TOOL_CALL, call_id=call_id, name=tool_name, input=args)
                try:
                    res = await fut  # resolved by the stdin reader on CMD_CLIENT_TOOL_RESULT
                except asyncio.CancelledError:
                    return {"content": [{"type": "text", "text": "client tool cancelled (session ended)"}], "is_error": True}
                finally:
                    pending.pop(call_id, None)
                res = res if isinstance(res, dict) else {}
                raw = res.get("content", "")
                # Pass content blocks through so a client tool can return pixels (screenshots /
                # computer-use), converting Anthropic image blocks to the MCP tool-result shape.
                # A bare string is wrapped as one text block.
                items = raw if isinstance(raw, list) else [{"type": "text", "text": str(raw)}]
                return {"content": [_to_mcp_block(b) for b in items], "is_error": bool(res.get("is_error"))}

            return handler

        sdk_tools.append(tool(name, desc, schema)(_make(name)))
        names.append(f"mcp__client__{name}")

    if not sdk_tools:
        return None, []
    return create_sdk_mcp_server(name="client", version="1.0.0", tools=sdk_tools), names
