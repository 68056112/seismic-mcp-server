"""ทดสอบ server แบบ end-to-end ผ่าน Streamable HTTP ด้วย MCP client อย่างเป็นทางการ

    pip install mcp==2.3.0
    python test_client.py                     # ค่าเริ่มต้น http://localhost:8000/mcp
    python test_client.py http://host:port/mcp
"""

import asyncio
import json
import sys

from mcp import Client, MCPError

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"


def summary(result) -> dict:
    data = result.structured_content
    if isinstance(data, dict) and "events" in data:
        return {"query": data["query"], "n_events": len(data["events"]),
                "first": {k: v for k, v in data["events"][0].items() if k != "raw"} if data["events"] else None}
    return data


async def main() -> None:
    async with Client(URL) as client:
        tools = await client.list_tools()
        print("tools:", [t.name for t in tools.tools])

        calls = [
            ("usgs_search_earthquakes", {"starttime": "2026-03-01", "endtime": "2026-03-07", "limit": 5}),
            ("tmd_get_daily_earthquakes", {}),
            ("tmd_get_recent_earthquakes", {"days": 3}),
            ("match_events_to_assets", {
                "events": [{"id": "e1", "magnitude": 5.2, "latitude": 18.79, "longitude": 98.98}],
                "assets": [{"asset_id": "CNX-01", "name": "Chiang Mai DC", "latitude": 18.9, "longitude": 99.2}],
            }),
            ("compute_parametric_payout", {
                "event": {"id": "e1", "magnitude": 5.7, "latitude": 18.79, "longitude": 98.98},
                "assets": [{"asset_id": "CNX-01", "name": "Warehouse A", "latitude": 18.9,
                            "longitude": 99.2, "sum_insured": 1_000_000}],
            }),
        ]
        for name, args in calls:
            res = await client.call_tool(name, args)
            print(f"\n== {name}\n", json.dumps(summary(res), ensure_ascii=False, indent=2)[:1500])

        try:
            await client.call_tool("usgs_search_earthquakes", {"starttime": "2026-03-01"})
        except MCPError as e:
            print(f"\n== validation error (expected): code={e.code} message={e.message}")


if __name__ == "__main__":
    asyncio.run(main())
