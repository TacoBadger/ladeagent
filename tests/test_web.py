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


def test_live_store_asks_the_source_every_time_and_falls_back_to_last_good(monkeypatch, tmp_path):
    """Cachen har samme nøgle hele døgnet. Live skal derfor hente igen, ellers mangler morgendagens priser."""
    import requests
    from ladeagent import config
    from ladeagent.data import eds

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    calls = []

    class Reply:
        def __init__(self, hour): self.hour = hour
        def raise_for_status(self): pass
        def json(self):
            return {"records": [{"PriceArea": "DK2", "TimeDK": f"2026-09-29T{self.hour}:00:00", "Minutes5DK": f"2026-09-29T{self.hour}:00:00",
                                 "DayAheadPriceDKK": 1000.0, "DayAheadPriceEUR": 134.0, "CO2Emission": 50.0,
                                 "OffshoreWindPower": 1.0, "OnshoreWindPower": 1.0, "SolarPower": 1.0,
                                 "ProductionGe100MW": 1.0, "ProductionLt100MW": 1.0}]}

    def get(url, params, timeout):
        calls.append(url)
        return Reply("10" if len(calls) <= 3 else "23")

    monkeypatch.setattr(eds.requests, "get", get)
    assert eds.DataStore.live().coverage()["prices"]["to"].endswith("T10:00:00")
    assert eds.DataStore.live().coverage()["prices"]["to"].endswith("T23:00:00")   # nyt kald, nye data
    assert len(calls) == 6

    def down(url, params, timeout):
        raise requests.ConnectionError("nede")

    monkeypatch.setattr(eds.requests, "get", down)
    assert eds.DataStore.live().coverage()["prices"]["to"].endswith("T23:00:00")   # sidste gode hentning
