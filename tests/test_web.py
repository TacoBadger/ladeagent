"""Forsidens JSON-endpoints: samme tools, samme grænser, kun læsning."""
import pytest
from starlette.testclient import TestClient

from ladeagent import tools, web
from ladeagent.data.eds import DataStore
from ladeagent.mcp_server import mcp

NIGHT = {"area": "DK2", "start": "2026-09-14T22:00", "end": "2026-09-15T07:00"}


@pytest.fixture(scope="module")
def store() -> DataStore:
    return DataStore.from_snapshot()


def test_api_gives_the_same_answer_as_the_tool(store):
    code, body = web.call_tool(store, "find_cheapest_window", {**NIGHT, "kwh": "40", "max_kw": "11"})
    assert code == 200
    assert body == tools.find_cheapest_window(store, **NIGHT, kwh=40, max_kw=11)
    assert body["best_window"]["start"] == "2026-09-15T02:00"


def test_api_accepts_danish_decimal_comma(store):
    code, body = web.call_tool(store, "find_cheapest_window", {**NIGHT, "kwh": "20", "max_kw": "3,7"})
    assert code == 200 and body["max_kw"] == 3.7


def test_api_has_no_write_tool(store):
    for name in ("create_charging_plan", "get_plan_status", "approve"):
        code, body = web.call_tool(store, name, NIGHT)
        assert code == 404 and "error" in body


@pytest.mark.parametrize("query", [
    {**NIGHT, "area": "SE3"},                                   # ukendt prisområde
    {**NIGHT, "kwh": "9999", "max_kw": "11"},                   # over kWh-loftet
    {**NIGHT, "kwh": "40", "max_kw": "nan"},                    # ikke et tal
    {**NIGHT, "kwh": "40", "max_kw": "11", "approve": "true"},  # ukendt parameter
    {"area": "DK2", "kwh": "40", "max_kw": "11"},               # mangler tidsrum
    {**NIGHT, "end": "2026-10-15T07:00", "kwh": "40", "max_kw": "11"},  # over 7 dage
])
def test_api_rejects_input_outside_the_limits(store, query):
    code, body = web.call_tool(store, "find_cheapest_window", query)
    assert code == 400 and set(body) == {"error"}


def test_http_routes_are_mounted_next_to_mcp():
    with TestClient(mcp.http_app(path="/mcp")) as client:
        ok = client.get("/api/tools/get_day_ahead_prices", params={"area": "DK1", "start": "2026-09-14T00:00", "end": "2026-09-15T00:00"})
        assert ok.status_code == 200 and len(ok.json()["hours"]) == 24
        assert client.get("/api/tools/create_charging_plan", params=NIGHT).status_code == 404
        assert client.post("/api/tools/get_day_ahead_prices").status_code == 405
        status = client.get("/api/status").json()
        assert status["read_tools"] == list(tools.READ_TOOLS) and status["coverage"]["prices"]["from"]
