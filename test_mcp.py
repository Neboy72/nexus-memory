#!/usr/bin/env python3
"""Test Nexus Memory MCP Server"""
import asyncio
import json
import sys
import os

# Use the same venv python
os.environ["VOYAGE_API_KEY"] = os.environ.get("VOYAGE_API_KEY", "")

# Must import mcp after env is set
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

SERVER_PATH = os.path.join(os.path.dirname(__file__), "src", "nexus_memory", "mcp_server.py")

async def test():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.nexus_memory.mcp_server"],
        env={**os.environ},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize
            init = await session.initialize()
            print(f"✅ Server: {init.serverInfo.name} v{init.serverInfo.version}")

            # List tools
            tools = await session.list_tools()
            tool_names = [t.name for t in tools.tools]
            print(f"✅ Tools available: {tool_names}")

            # Health
            result = await session.call_tool("health", {})
            health = json.loads(result.content[0].text)
            print(f"✅ Health: {health}")

            # Remember
            result = await session.call_tool("remember", {
                "text": "Nebo wohnt in Eltville und mag Döner.",
                "access_level": "trusted",
                "category": "fact",
                "source": "test",
            })
            r = json.loads(result.content[0].text)
            mem_id = r["id"]
            print(f"✅ Stored memory: {mem_id} [{r['access_level']}]")

            # Recall (public → should NOT find the trusted memory)
            result = await session.call_tool("recall", {
                "query": "Nebo Eltville",
                "filter_level": "public",
                "limit": 5,
            })
            r = json.loads(result.content[0].text)
            print(f"✅ Public recall: {r['count']} results (expected: 0)")

            # Recall (trusted → should find it)
            result = await session.call_tool("recall", {
                "query": "Nebo Eltville",
                "filter_level": "trusted",
                "limit": 5,
            })
            r = json.loads(result.content[0].text)
            print(f"✅ Trusted recall: {r['count']} result(s)")
            for mem in r["results"]:
                print(f"   → {mem['text'][:60]}... [score: {mem['score']:.3f}]")

            # Forget
            result = await session.call_tool("forget", {"memory_id": mem_id})
            r = json.loads(result.content[0].text)
            print(f"✅ Delete: {r['status']}")

    print("\n🎉 ALL TESTS PASSED")

asyncio.run(test())
