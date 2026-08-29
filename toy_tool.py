import asyncio
from claude_agent_sdk import (
    tool,
    create_sdk_mcp_server,
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
)

@tool("add", "Add two numbers together", {"a": float, "b": float})
async def add(args):
    result = args["a"] + args["b"]
    return {"content": [{"type": "text", "text": f"The sum is {result}"}]}

calculator = create_sdk_mcp_server(name="calc", tools=[add])

async def main():
    options = ClaudeAgentOptions(
        mcp_servers={"calc": calculator},
        allowed_tools=["mcp__calc__add"],
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query("What is 47 plus 89? Use your tool to calculate it.")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print("Claude says:", block.text)
                    elif isinstance(block, ToolUseBlock):
                        print(f"Claude called tool: {block.name} with input {block.input}")

asyncio.run(main())