"""Audit trail for ladeplaner (runde 2).

Hver gang agenten opretter en ladeplan, skrives én linje til data/audit.jsonl med
tid, model, prompt-version, trace-id, alle tool-kald (navn, argumenter og sha256 af
svaret), pris og sha256 af de inputdata planen blev regnet på. Loggen er nok til at
rekonstruere planen senere: kør de samme tool-kald på de samme data og sammenlign.
"""
from __future__ import annotations

import hashlib
import json
import random
import time

from ladeagent import config, plans, tools
from ladeagent.data.eds import DataStore


def _canonical(x):
    """Tal som float (MCP-laget giver 40.0 hvor agenten giver 40), nøgler sorteret."""
    if isinstance(x, bool):
        return x
    if isinstance(x, int):
        return float(x)
    if isinstance(x, dict):
        return {k: _canonical(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_canonical(v) for v in x]
    return x


def result_sha256(result) -> str:
    """Stabil hash af et tool-svar (dict), uafhængig af nøglerækkefølge og int/float-forskelle."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            return hashlib.sha256(result.encode()).hexdigest()
    return hashlib.sha256(json.dumps(_canonical(result), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def record_plan(*, plan: dict, trace_id: str, model: str, prompt_version: str, backend: str, question: str,
                tool_calls: list[dict], cost_usd: float, data_sha256: str) -> dict:
    entry = {
        "plan_id": plan["plan_id"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "trace_id": trace_id,
        "model": model, "prompt_version": prompt_version, "backend": backend, "question": question,
        "tool_calls": [{"name": tc["name"], "args": tc["args"], "result_sha256": result_sha256(tc.get("result", ""))}
                       for tc in tool_calls if tc.get("executed", True)],
        "cost_usd": round(cost_usd, 6), "cost_dkk": round(cost_usd * config.USD_TO_DKK, 4),
        "data_sha256": data_sha256, "plan": plan,
    }
    config.AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with config.AUDIT_FILE.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load() -> list[dict]:
    if not config.AUDIT_FILE.exists():
        return []
    return [json.loads(l) for l in config.AUDIT_FILE.read_text().splitlines() if l.strip()]


def reconstruct(entry: dict, store: DataStore) -> dict:
    """Rekonstruerer en plan fra loggen alene: samme data, samme tool-kald, samme svar?

    Fire kontroller, alle skal holde:
      data_hash      inputdata er de samme som da planen blev lavet
      tool_replay    hvert read-tool-kald giver i dag et svar med samme sha256
      plan_on_disk   planen i plans.json har samme felter som i loggen
      window_derived planens vindue er det, find_cheapest_window giver (hvis det blev kaldt)
    """
    checks: dict[str, bool] = {}
    checks["data_hash"] = store.sha256() == entry["data_sha256"]
    replay_ok = True
    derived_window = None
    for tc in entry["tool_calls"]:
        if tc["name"] in tools.READ_TOOLS:
            try:
                out = tools.READ_TOOLS[tc["name"]]["fn"](store, **tc["args"])
            except tools.ToolError as e:
                out = {"error": str(e)}
            if result_sha256(out) != tc["result_sha256"]:
                replay_ok = False
            if tc["name"] == "find_cheapest_window" and "best_window" in out:
                derived_window = out["best_window"]
    checks["tool_replay"] = replay_ok
    on_disk = plans._load().get(entry["plan_id"])
    keys = ("plan_id", "status", "area", "start", "end", "kwh", "max_kw")
    checks["plan_on_disk"] = on_disk is not None and all(on_disk.get(k) == entry["plan"].get(k) for k in keys if k != "status")
    if derived_window is not None:
        checks["window_derived"] = (entry["plan"]["start"] == derived_window["start"] and entry["plan"]["end"] == derived_window["end"])
    else:
        checks["window_derived"] = False  # planen blev oprettet uden find_cheapest_window; kan ikke udledes
    return {"plan_id": entry["plan_id"], "trace_id": entry["trace_id"], "ok": all(checks.values()), "checks": checks,
            "plan": entry["plan"], "cost_dkk": entry["cost_dkk"], "model": entry["model"]}


def reconstruct_random(store: DataStore, seed: int | None = None) -> dict:
    entries = load()
    if not entries:
        return {"ok": False, "error": "audit-loggen er tom"}
    rnd = random.Random(seed)
    out = reconstruct(rnd.choice(entries), store)
    out["candidates"] = len(entries)
    return out
