# MCP-main/myserver/server.py
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import logger

import glob, os
from .types import MarkdownResource
from .utils import uri2path
from .config import VAULT_PATH

class KnowledgeVaultServer:
    def __init__(self):
        self.app = FastMCP("knowledge-vault")
        self.resource_map = {}
        self._init_resources()
        self._init_tools()

    def _init_resources(self):
        print(f"[DEBUG] Looking for markdown in: {VAULT_PATH}")
        file_list = glob.glob(os.path.join(str(VAULT_PATH), '**/*.md'), recursive=True)
        print(f"[DEBUG] Found files: {file_list}")
        self.resource_map = {}

        for path in file_list:
            path = path.lower()  # XOS file system is case-insensitive
            uri = f"file://{os.path.relpath(path, start=str(VAULT_PATH))}".lower()

            rsrc = MarkdownResource(
                uri=uri,
                name=os.path.basename(path).split('.')[0],
                mime_type="text/markdown",
                size=os.path.getsize(path),
            )
            self.resource_map[uri] = rsrc
            print(f"[DEBUG] Registered URI: {uri}")
            self.app.add_resource(rsrc)

    def _init_tools(self):
        @self.app.tool()
        async def list_knowledges() -> list[dict[str, str]]:
            """List the names and URIs of all knowledges written in the vault."""
            return [
                {'name': rsrc.name, 'uri': rsrc.uri, 'size': rsrc.size}
                for rsrc in self.resource_map.values()
            ]

        @self.app.tool()
        async def get_knowledge_by_uri(uri: str) -> str:
            """Get contents of the knowledge resource by URI."""
            uri = uri2path(uri)
            print(f"[DEBUG] Looking for URI: {uri}")
            rsrc = self.resource_map.get(uri, None)
            if not rsrc:
                raise ValueError("Not registered resource URI")
            return await rsrc.read()

    def run(self, transport: str = "stdio", **_kwargs):
        """
        Your installed fastmcp FastMCP.run() doesn't accept host/port.
        Always run in stdio mode so the client can spawn us.
        """
        self.app.run(transport="stdio")
