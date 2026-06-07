#!/usr/bin/env python3
"""Minimal test — show what recall returns"""
import asyncio, json, os, sys

from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

async def test():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.nexus_memory.mcp_server"],
        env={**os.environ},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Remember something public
            r = await session.call_tool("remember", {
                "text": "Test-Eintrag: Berlin ist Hauptstadt von Deutschland.",
                "access_level": "public",
                "category": "fact",
            })
            print("remember response type:", type(r))
            print("remember content:", r.content)
            if r.content:
                c = r.content[0]
                print("content[0] type:", type(c))
                print("content[0] text:", repr(getattr(c, 'text', 'NO TEXT ATTR')))
                data = json.loads(c.text)
                print("✅ Remember OK:", data)

            # Recall
            r2 = await session.call_tool("recall", {
                "query": "Berlin Hauptstadt",
                "filter_level": "public",
                "limit": 5,
            })
            print("\nrecall response type:", type(r2))
            print("recall content:", r2.content)
            if r2.content:
                c = r2.content[0]
                print("content[0] type:", type(c))
                print("content[0] text:", repr(getattr(c, 'text', 'NO TEXT ATTR')))
                if hasattr(c, 'text') and c.text:
                    data = json.loads(c.text)
                    print("✅ Recall OK:", data["count"], "results")
                    for m in data["results"]:
                        print(f"   → {m['text'][:50]}...")

            print("\n🎉 Done")
            await session.call_tool("forget", {"memory_id": data.get("id", "")})

asyncio.run(test())
