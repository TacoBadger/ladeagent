import json

import pytest
from fastmcp import Client

from ladeagent.mcp_server import mcp


@pytest.mark.asyncio
async def test_mcp_lists_tools_with_correct_read_only_annotations():
    async with Client(mcp) as c:
        tools = {t.name: t for t in await c.list_tools()}
    assert set(tools) == {"get_day_ahead_prices", "find_cheapest_window", "estimate_cost", "get_co2_intensity",
                          "get_generation_mix", "create_charging_plan", "get_plan_status"}
    for name, t in tools.items():
        ro = t.annotations.read_only_hint
        assert ro is (name != "create_charging_plan"), name


@pytest.mark.asyncio
async def test_mcp_call_and_validation():
    async with Client(mcp) as c:
        r = await c.call_tool("find_cheapest_window", {"area": "DK2", "start": "2026-09-14T22:00", "end": "2026-09-15T07:00", "kwh": 40, "max_kw": 11})
        assert json.loads(r.content[0].text)["best_window"]["cost_dkk"] > 0
        bad = await c.call_tool("get_day_ahead_prices", {"area": "SE3", "start": "2026-09-14T00:00", "end": "2026-09-15T00:00"}, raise_on_error=False)
        assert "Ukendt prisområde" in bad.content[0].text
        # pydantic-grænse i MCP-laget: kwh > MAX_KWH afvises før tool'et køres
        over = await c.call_tool("find_cheapest_window", {"area": "DK1", "start": "2026-09-14T22:00", "end": "2026-09-15T07:00", "kwh": 9999, "max_kw": 11}, raise_on_error=False)
        assert over.is_error
