# MCP-main/run_server.py
import os
from myserver import MeteoServer

if __name__ == "__main__":
    srv = MeteoServer()
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    srv.run(transport=transport)
