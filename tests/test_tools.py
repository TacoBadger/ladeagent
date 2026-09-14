"""Deterministiske tests på snapshottet. Ingen model, ingen netværk."""
import json
import math

import pytest

from ladeagent import config, plans, tools
from ladeagent.agent import build_tools
from ladeagent.data.eds import DataStore


@pytest.fixture(scope="module")
def store():
    return DataStore.from_snapshot()


def test_snapshot_covers_dk1_dk2(store):
    assert set(store.prices["area"]) == {"DK1", "DK2"}
    cov = store.coverage()["prices"]
    assert cov["from"].startswith(config.SNAPSHOT_START)


def test_cheapest_window_is_actually_cheapest(store):
    r = tools.find_cheapest_window(store, "DK2", "2026-09-14T22:00", "2026-09-15T07:00", kwh=40, max_kw=11)
    assert r["hours_needed"] == math.ceil(40 / 11) == 4
    # brute force-kontrol
    p = tools.get_day_ahead_prices(store, "DK2", "2026-09-14T22:00", "2026-09-15T07:00")["hours"]
    per_hour = [11, 11, 11, 40 - 33]
    costs = [sum(p[i + k]["dkk_per_kwh"] * per_hour[k] for k in range(4)) for i in range(len(p) - 3)]
    assert r["best_window"]["cost_dkk"] == pytest.approx(min(costs), abs=0.01)
    assert r["worst_window"]["cost_dkk"] == pytest.approx(max(costs), abs=0.01)
    assert r["savings_vs_now_dkk"] == pytest.approx(costs[0] - min(costs), abs=0.01)


def test_estimate_cost_matches_prices(store):
    r = tools.estimate_cost(store, "DK1", "2026-09-15T17:00", "2026-09-15T20:00", kw=2.0)
    p = tools.get_day_ahead_prices(store, "DK1", "2026-09-15T17:00", "2026-09-15T20:00")["hours"]
    assert r["hours"] == 3 and r["kwh"] == 6.0
    assert r["cost_dkk"] == pytest.approx(sum(h["dkk_per_kwh"] * 2 for h in p), abs=0.01)


def test_every_amount_carries_the_spot_price_caveat(store):
    for fn, kw in ((tools.get_day_ahead_prices, {}), (tools.estimate_cost, {"kw": 1}), (tools.find_cheapest_window, {"kwh": 10, "max_kw": 5})):
        r = fn(store, "DK1", "2026-09-14T00:00", "2026-09-15T00:00", **kw)
        assert "ekskl. nettarif, elafgift og moms" in r["note"]


@pytest.mark.parametrize("area", ["SE3", "DE", "dk3", ""])
def test_unknown_area_rejected(store, area):
    with pytest.raises(tools.ToolError):
        tools.get_day_ahead_prices(store, area, "2026-09-14T00:00", "2026-09-15T00:00")


def test_window_limits(store):
    with pytest.raises(tools.ToolError, match="7 dage"):
        tools.get_day_ahead_prices(store, "DK1", "2026-09-01T00:00", "2026-09-15T00:00")
    with pytest.raises(tools.ToolError, match="efter start"):
        tools.get_day_ahead_prices(store, "DK1", "2026-09-15T00:00", "2026-09-14T00:00")
    with pytest.raises(tools.ToolError):
        tools.find_cheapest_window(store, "DK1", "2026-09-14T22:00", "2026-09-15T07:00", kwh=9999, max_kw=11)
    with pytest.raises(tools.ToolError):
        tools.find_cheapest_window(store, "DK1", "2026-09-14T22:00", "2026-09-15T07:00", kwh=20, max_kw=500)


def test_missing_data_is_an_honest_error_not_a_guess(store):
    with pytest.raises(tools.ToolError, match="Data findes for"):
        tools.estimate_cost(store, "DK1", "2026-09-20T01:00", "2026-09-20T04:00", kw=2)


def test_generation_mix_shares_sum_to_100(store):
    r = tools.get_generation_mix(store, "DK1", "2026-09-13T00:00", "2026-09-14T00:00")
    assert sum(r["shares_pct"].values()) == pytest.approx(100, abs=0.5)
    assert 0 <= r["wind_and_solar_pct"] <= 100


def test_model_cannot_approve_plans(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PLANS_FILE", tmp_path / "plans.json")
    r = plans.create_charging_plan("DK2", "2026-09-15T02:00", "2026-09-15T06:00", 40, 11)
    assert r["status"] == "pending"
    assert "approve" not in {t["name"] for t in build_tools()}  # ikke eksponeret til modellen
    assert plans.get_plan_status(r["plan_id"])["status"] == "pending"
    assert plans.approve_plan(r["plan_id"], actor="test-human")["status"] == "approved"


def test_tool_definitions_are_strict_and_bounded():
    for t in build_tools():
        assert t["strict"] is True
        assert t["input_schema"]["additionalProperties"] is False
        if "area" in t["input_schema"]["properties"]:
            assert t["input_schema"]["properties"]["area"]["enum"] == ["DK1", "DK2"]
    json.dumps(build_tools())  # serialiserbart til API'et
