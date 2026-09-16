#!/usr/bin/env python3
"""Test Nexus Memory MCP Server"""
import asyncio
import json
import sys
import os

# H259: fail fast instead of masking a missing credential. The old
# ``os.environ.setdefault("VOYAGE_API_KEY", "")`` made the key *exist but empty*,
# so the server started in a degraded mode and the test could pass on a
# misconfiguration. An honest legacy test must refuse to run.
if not os.environ.get("VOYAGE_API_KEY"):
    print(
        "VOYAGE_API_KEY is not set — refusing to run the MCP test against a "
        "degraded server. Export it first:\n"
        "  export VOYAGE_API_KEY=...",
        file=sys.stderr,
    )
    sys.exit(1)

# Must import mcp after env is set
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp import ClientSession

# H258: repo root derived from this file (scripts/legacy/test_mcp.py → three
# levels up). ``-m src.nexus_memory.mcp_server`` only resolves when the CWD is
# the repo root, so it is passed explicitly below.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _text(result) -> str:
    """Return the text payload of a tool response, or raise AssertionError.

    H260: a response with empty content or an error payload used to explode
    with IndexError/KeyError somewhere further down the test. Fail with the
    raw response instead.
    """
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise AssertionError(f"tool returned an error response: {result!r}")
    content = getattr(result, "content", None)
    if not content:
        raise AssertionError(f"tool returned an empty response: {result!r}")
    return content[0].text


async def test():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.nexus_memory.mcp_server"],
        # cwd + PYTHONPATH pin the module path to the repo root so the script
        # runs from any directory (H258).
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                [REPO_ROOT, os.environ.get("PYTHONPATH", "")]
            ).strip(os.pathsep),
        },
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
            health = json.loads(_text(result))
            print(f"✅ Health: {health}")

            # Remember. W33-11: the call AND the id extraction live inside the
            # guarded block — a response without "id" (or a non-JSON payload)
            # used to escape before the finally was ever entered, so `forget`
            # never ran and the test memory leaked into the store. `mem_id`
            # stays None until the id is actually known.
            mem_id = None
            # Nr 261: forget must ALWAYS run, even if an assert below fails
            try:
                result = await session.call_tool("remember", {
                    "text": "Test user lives in a city and likes food.",
                    "access_level": "trusted",
                    "category": "fact",
                    "source": "test",
                })
                r = json.loads(_text(result))
                mem_id = r["id"]
                print(f"✅ Stored memory: {mem_id} [{r['access_level']}]")

                # Recall (public → should NOT find the trusted memory)
                result = await session.call_tool("recall", {
                    "query": "test user city",
                    "filter_level": "public",
                    "limit": 5,
                })
                r = json.loads(_text(result))
                print(f"✅ Public recall: {r['count']} results (expected: 0)")
                assert r["count"] == 0, f"public recall leaked: {r}"

                # Recall (trusted → should find it)
                result = await session.call_tool("recall", {
                    "query": "test user city",
                    "filter_level": "trusted",
                    "limit": 5,
                })
                r = json.loads(_text(result))
                print(f"✅ Trusted recall: {r['count']} result(s)")
                assert r["count"] >= 1, f"trusted recall found nothing: {r}"
                for mem in r["results"]:
                    print(f"   → {mem['text'][:60]}... [score: {mem['score']:.3f}]")
            finally:
                # Forget. W33-11: cleanup must never MASK the real defect — a
                # raising call_tool/_text/json.loads used to replace the
                # in-flight AssertionError and lose the original traceback.
                # Cleanup problems are reported on stderr instead, and the
                # delete status is checked (the handler answers
                # {"status": "not_found"} / "error" without raising).
                if mem_id is None:
                    print("⚠️ no memory id — nothing to clean up", file=sys.stderr)
                else:
                    try:
                        result = await session.call_tool("forget", {"memory_id": mem_id})
                        r = json.loads(_text(result))
                        if r.get("status") != "deleted":
                            print(f"⚠️ delete did not succeed: {r}", file=sys.stderr)
                        else:
                            print(f"✅ Delete: {r['status']}")
                    except Exception as exc:
                        print(
                            f"⚠️ cleanup failed (test verdict above stands): {exc!r}",
                            file=sys.stderr,
                        )

    print("\n🎉 ALL TESTS PASSED")

if __name__ == "__main__":
    # Nr 445: the asyncio.run() at import time made this module un-importable
    # (any import would launch the MCP server session as a side effect).
    asyncio.run(test())
