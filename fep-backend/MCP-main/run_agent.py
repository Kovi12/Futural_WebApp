# MCP-main/run_agent.py
import os
import asyncio
from myagent import Agent, LlamaPrompt, HFModel  # LlamaCPP available if installed

async def run_agent():
    # Use HF-based model by default (no llama_cpp required)
    model_id = os.getenv("MCP_HF_MODEL", "meta-llama/Llama-3.1-8B-Instruct")
    model = HFModel(model_id=model_id)

    prompt = LlamaPrompt()
    agent = Agent(name="knowledge-agent", model=model, prompt=prompt)

    # Prefer WebSocket server the app launches
    ws = f"ws://{os.getenv('MCP_HOST','127.0.0.1')}:{int(os.getenv('MCP_PORT','6072'))}/"
    agent.register_mcp(path=ws)

    async with agent:
        while (q := input('(prompt) ')) != 'bye':
            responses = await agent.chat(q)
            for r in responses:
                print(f"({r.type}) {r.data}")

if __name__ == '__main__':
    asyncio.run(run_agent())
