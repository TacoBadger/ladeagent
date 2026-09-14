"""MCP-server der eksponerer ladeagentens tools.

Kør:  python -m ladeagent.mcp_server            (stdio, til Claude Desktop / Claude Code / MCP Inspector)
      python -m ladeagent.mcp_server --live     (live data fra Energi Data Service i stedet for snapshot)

Adgangsprincip:
  - Fem read-only tools (annotations.readOnlyHint = true).
  - Ét write-tool, der kun kan oprette et FORSLAG (pending). Godkendelse sker
    uden for modellen (cli.py approve). Der findes ikke et approve-tool.
  - Alle input valideres med pydantic (områder, tidsvinduer, kWh/kW-grænser)
    før noget regnes, og fejl går tilbage som tekst, ikke som stack traces.
"""
from __future__ import annotations

import json
import sys
from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

from ladeagent import config, plans, tools
from ladeagent.data.eds import DataStore

mcp = FastMCP(
    name="ladeagent",
    instructions=(
        "Tools over danske elpriser (DK1/DK2) fra Energinet's Energi Data Service. "
        "Alle priser er spotpriser ekskl. tariffer og afgifter. Tools kan ikke se kundens elaftale. "
        "create_charging_plan opretter kun et forslag; godkendelse sker af et menneske uden for MCP."
    ),
)

_STORE: DataStore | None = None


def store() -> DataStore:
    global _STORE
    if _STORE is None:
        _STORE = DataStore.live() if "--live" in sys.argv else DataStore.from_snapshot()
    return _STORE


def _run(fn, **kwargs) -> str:
    try:
        return json.dumps(fn(store(), **kwargs), ensure_ascii=False)
    except tools.ToolError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


Area = Annotated[str, Field(description="DK1 (vest for Storebælt) eller DK2 (øst for Storebælt).")]
Start = Annotated[str, Field(description="Start, lokal dansk tid, ISO-8601, fx 2026-09-15T22:00.")]
End = Annotated[str, Field(description="Slut (eksklusiv), lokal dansk tid, ISO-8601. Max 7 dage efter start.")]
RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


@mcp.tool(name="get_day_ahead_prices", description=tools.READ_TOOLS["get_day_ahead_prices"]["description"], annotations=RO)
def get_day_ahead_prices(area: Area, start: Start, end: End) -> str:
    return _run(tools.get_day_ahead_prices, area=area, start=start, end=end)


@mcp.tool(name="find_cheapest_window", description=tools.READ_TOOLS["find_cheapest_window"]["description"], annotations=RO)
def find_cheapest_window(
    area: Area, start: Start, end: End,
    kwh: Annotated[float, Field(gt=0, le=config.MAX_KWH, description="Energi der skal lades, kWh.")],
    max_kw: Annotated[float, Field(gt=0, le=config.MAX_KW, description="Ladeeffekt, kW. Typisk 11 for en ladeboks.")],
) -> str:
    return _run(tools.find_cheapest_window, area=area, start=start, end=end, kwh=kwh, max_kw=max_kw)


@mcp.tool(name="estimate_cost", description=tools.READ_TOOLS["estimate_cost"]["description"], annotations=RO)
def estimate_cost(area: Area, start: Start, end: End, kw: Annotated[float, Field(gt=0, le=config.MAX_KW, description="Konstant effekt, kW.")]) -> str:
    return _run(tools.estimate_cost, area=area, start=start, end=end, kw=kw)


@mcp.tool(name="get_co2_intensity", description=tools.READ_TOOLS["get_co2_intensity"]["description"], annotations=RO)
def get_co2_intensity(area: Area, start: Start, end: End) -> str:
    return _run(tools.get_co2_intensity, area=area, start=start, end=end)


@mcp.tool(name="get_generation_mix", description=tools.READ_TOOLS["get_generation_mix"]["description"], annotations=RO)
def get_generation_mix(area: Area, start: Start, end: End) -> str:
    return _run(tools.get_generation_mix, area=area, start=start, end=end)


@mcp.tool(
    name="create_charging_plan",
    description=plans.WRITE_TOOLS["create_charging_plan"]["description"],
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def create_charging_plan(
    area: Area, start: Start, end: End,
    kwh: Annotated[float, Field(gt=0, le=config.MAX_KWH)],
    max_kw: Annotated[float, Field(gt=0, le=config.MAX_KW)],
    customer_note: Annotated[str, Field(max_length=200, description="Kort note fra kunden.")] = "",
) -> str:
    try:
        return json.dumps(plans.create_charging_plan(area, start, end, kwh, max_kw, customer_note), ensure_ascii=False)
    except tools.ToolError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(name="get_plan_status", description=plans.WRITE_TOOLS["get_plan_status"]["description"], annotations=RO)
def get_plan_status(plan_id: str) -> str:
    try:
        return json.dumps(plans.get_plan_status(plan_id), ensure_ascii=False)
    except tools.ToolError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()  # stdio
