# MCP-main/myagent/agent.py
from __future__ import annotations
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import AsyncGenerator, Optional

from .prompt import BasePrompt, LlamaPrompt
from .model import BaseModel
from .client import MCPClientManager    
from .types import AgentResponse
from . import utils

log = logging.getLogger("myagent.agent")

SYSTEM_PROMPT = "You are a helpful assistant"

TOOL_CALL_PROMPT = """You are an expert in composing functions. You are given a question and a set of possible functions.
Based on the question, you will need to make one or more function/tool calls to achieve the purpose.
If none of the function can be used, point it out. If the given question lacks the parameters required by the function,
also point it out. You should only return the function call in tools call sections.

If you decide to invoke any of the function(s), you MUST put it in the format of [func_name1(), func_name2(params_name1=params_value1, params_name2=params_value2...), func_name3(params)]
You SHOULD NOT include any other text in the response.

Here is a list of functions in JSON format that you can invoke.

{function_scheme}
"""

@dataclass
class Agent:
    name: str
    model: BaseModel
    prompt: BasePrompt
    # Provide endpoint via env or register_mcp(); no default hard fail
    mcp_endpoint: str = field(default_factory=lambda: os.environ.get("MCP_ENDPOINT", ""))
    mcp_manager: MCPClientManager = field(init=False)

    # internals
    func_scheme_prompt: str = field(init=False, default="")
    resource_prompt: str = field(init=False, default="")
    tool_pattern: re.Pattern = field(init=False, default=re.compile(r'\[([A-Za-z0-9_]+\(([A-Za-z0-9_]+=\"?.+\"?,?\s?)*\),?\s?)+\]'))
    func_pattern: re.Pattern = field(init=False, default=re.compile(r'(?P<function>[A-Za-z0-9_]+)\((?P<params>[A-Za-z0-9_]+=\"?.+\"?,?\s?)*\)'))

    def __post_init__(self) -> None:
        self.mcp_manager = MCPClientManager(self.mcp_endpoint)

    @property
    def model_name(self):
        return getattr(self.model, "model_id", getattr(self.model, "name", "unknown"))

    @property
    def server_list(self):
        # optional; your manager can expose names if desired
        return ["default"]

    def register_mcp(self, path: str) -> None:
        """Set/override the MCP endpoint. For stdio use a command string like 'python3 /path/run_server.py'."""
        self.mcp_manager.set_endpoint(path)

    async def init_agent(self) -> None:
        await self.mcp_manager.init_mcp_client()

        # Fetch tool schema & resources via MCP (your server exposes them)
        # Expect two tools: list_knowledges, get_knowledge_by_uri; adapt if you add more.
        sess = self.mcp_manager.session
        assert sess is not None

        tools = await sess.list_tools()
        resources = await sess.list_resources()

        func_scheme_list = [utils.tool2dict(t) for t in tools.tools]
        resource_list = [utils.resource2dict(r) for r in resources.resources]

        self.func_scheme_prompt = json.dumps(func_scheme_list, ensure_ascii=False)
        self.resource_prompt = json.dumps(resource_list, ensure_ascii=False)

        p = self.prompt.get_system_prompt(SYSTEM_PROMPT)
        self.prompt.set_system_prompt(p)

    async def clean_agent(self) -> None:
        await self.mcp_manager.aclose()

    async def __aenter__(self):
        await self.init_agent()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.clean_agent()

    def _is_tool_required(self, response: str) -> bool:
        # naive signal: any of the tool names appear in the response
        return any(d.get("name") in response for d in json.loads(self.func_scheme_prompt or "[]"))

    def _iter_func_calls(self, response: str):
        for signature in response.strip("[]").split(","):
            signature = signature.strip()
            if not signature:
                continue
            m = self.func_pattern.findall(signature)
            if m:
                name, param_string = m[0]
                yield name, utils.param2dict(param_string)

    async def _exec_tools(self, response: str):
        sess = self.mcp_manager.session
        assert sess is not None

        results = []
        for name, params in self._iter_func_calls(response):
            call = await sess.call_tool(name=name, arguments=params)
            # The mcp client returns a list of TextContent objects; convert to plain text
            texts = []
            for item in call.content:
                try:
                    texts.append(getattr(item, "text", str(item)))
                except Exception:
                    pass
            results.append({"name": name, "output": texts})
        return results

    async def chat(self, question: str, **kwargs) -> list[AgentResponse]:
        out: list[AgentResponse] = []

        tool_scheme = TOOL_CALL_PROMPT.format(function_scheme=self.func_scheme_prompt)
        user_msg = self.prompt.get_user_prompt(question=question, tool_scheme=tool_scheme)
        self.prompt.append_history(user_msg)

        # Step 1: tool planning
        first = self.model.generate(self.prompt.get_generation_prompt(tool_enabled=True), **kwargs)
        first = first.strip().lstrip('()<>{}`')

        if self._is_tool_required(first):
            out.append(AgentResponse(type="tool-calling", data=first))
            self.prompt.append_history(self.prompt.get_assistant_prompt(answer=first))

            # Step 2: execute tools via MCP
            tool_results = await self._exec_tools(first)
            tool_json = json.dumps(tool_results, ensure_ascii=False)
            out.append(AgentResponse(type="tool-result", data=tool_json))

            # Step 3: final answer using results
            self.prompt.append_history(self.prompt.get_tool_result_prompt(result=tool_json))
            final = self.model.generate(self.prompt.get_generation_prompt(tool_enabled=False, last=3), **kwargs)
        else:
            final = first

        out.append(AgentResponse(type="text", data=final))
        self.prompt.append_history(self.prompt.get_assistant_prompt(answer=final))
        return out
