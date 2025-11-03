# MCP-main/myagent/client.py
from __future__ import annotations

import logging, shlex
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

log = logging.getLogger("myagent.client")

try:
    from mcp import ClientSession, StdioServerParameters  # type: ignore
    from mcp.client.stdio import stdio_client             # type: ignore
    _HAS_MCP = True
except Exception as e:
    log.error("mcp stdio client not available: %s", e)
    _HAS_MCP = False
    ClientSession = None            # type: ignore
    StdioServerParameters = None    # type: ignore
    stdio_client = None             # type: ignore

try:
    from . import utils as _utils
except Exception:
    _utils = None


@dataclass
class MCPClientManager:
    endpoint: str = ""
    _stack: AsyncExitStack | None = field(default=None, init=False)
    session: ClientSession | None = field(default=None, init=False)  # type: ignore

    # keep Agent’s calls working
    def set_endpoint(self, path: str) -> None:
        self.endpoint = path

    def register_mcp(self, path: str) -> None:
        self.set_endpoint(path)

    async def init_mcp_client(self) -> None:
        if not _HAS_MCP:
            raise RuntimeError("mcp package (stdio) is not installed in this environment.")
        if not self.endpoint:
            raise RuntimeError("MCP endpoint/command is not set. Call register_mcp(path) first.")

        self._stack = AsyncExitStack()

        cmd = shlex.split(self.endpoint)
        if not cmd:
            raise RuntimeError("Empty MCP endpoint command.")

        # Newer mcp has (command=..., args=[...]); older wants a single string.
        try:
            params = StdioServerParameters(command=cmd[0], args=cmd[1:])  # type: ignore
        except Exception:
            params = StdioServerParameters(command=" ".join(cmd))         # type: ignore

        read, write = await self._stack.enter_async_context(stdio_client(params))  # type: ignore
        self.session = await self._stack.enter_async_context(ClientSession(read, write))  # type: ignore
        await self.session.initialize()

    async def clean_mcp_client(self) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    def get_server_names(self) -> List[str]:
        return [f"stdio:{self.endpoint}"] if self.endpoint else []

    async def get_func_scheme(self) -> List[Dict[str, Any]]:
        if not self.session:
            return []
        tools_resp = await self.session.list_tools()
        tools = getattr(tools_resp, "tools", []) or []
        if _utils:
            return [_utils.tool2dict(t) for t in tools]
        return [
            {
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", ""),
                "parameters": getattr(t, "inputSchema", {}) or {},
            }
            for t in tools
        ]

    async def get_resource_list(self) -> List[Dict[str, Any]]:
        if not self.session:
            return []
        res_resp = await self.session.list_resources()
        resources = getattr(res_resp, "resources", []) or []
        if _utils:
            return [_utils.resource2dict(r) for r in resources]
        return [
            {
                "uri": str(getattr(r, "uri", "")),
                "name": getattr(r, "name", ""),
                "mimeType": getattr(r, "mimeType", ""),
            }
            for r in resources
        ]

    async def call_tool(self, name: str, params: Dict[str, Any]) -> Tuple[bool, List[Any]]:
        if not self.session:
            return True, []
        try:
            resp = await self.session.call_tool(name, arguments=params)
            return bool(getattr(resp, "is_error", False)), getattr(resp, "content", []) or []
        except Exception as e:
            log.exception("call_tool(%s) failed: %s", name, e)
            return True, []

# keep legacy misspelling
MCPClientMaanger = MCPClientManager  # noqa: N816
