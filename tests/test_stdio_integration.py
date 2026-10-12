"""Real MCP initialization, tool listing and source read through the installed process."""
import asyncio
import json
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

def test_real_stdio_round_trip(sample_config):
    async def check():
        parameters = StdioServerParameters(command=sys.executable, args=[
            "-m", "dotnet_code_advisor", "--config", str(sample_config.source), "--no-roslyn",
        ])
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert len(listing.tools) == 15
                result = await session.call_tool("read_file_range", arguments={
                    "file": "Legacy/Shared/OrderRequest.cs", "line_start": 1, "line_end": 5,
                })
                assert not result.is_error
                content = json.loads(result.content[0].text)
                assert content["status"] == "ok"
                assert "OrderRequest" in content["text"]
    asyncio.run(asyncio.wait_for(check(), timeout=30))
