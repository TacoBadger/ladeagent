"""Scorer eksisterende rapporter igen med den aktuelle scoringslogik, uden at køre modellen.

  python -m evals.rescore                # alle evals/reports/*.json
  python -m evals.rescore v1_claude-opus-5_cli

Bruges når scoringen ændres (fx normalisering af tidsformat), så gamle og nye
kørsler er scoret ens. Dommer-resultater (tone) genbruges som de er; en case hvor
dommeren ikke svarede i JSON markeres "judge_error" og tæller som ikke bestået,
men fremgår tydeligt i rapporten.
"""
from __future__ import annotations

import json
import sys

from ladeagent.agent import RunResult, Usage
from evals.run_evals import GOLDEN, REPORTS, score_case, write_report


def rescore(label: str) -> None:
    path = REPORTS / f"{label}.json"
    data = json.loads(path.read_text())
    golden = {json.loads(l)["id"]: json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()}
    rows = []
    for old in data["rows"]:
        case = golden[old["id"]]
        res = RunResult(run_id="rescore", question=old["question"], model=data["summary"]["model"],
                        prompt_version=data["summary"]["prompt_version"], parsed=old["parsed"] or None,
                        raw_text=json.dumps(old["parsed"]) if old["parsed"] else "")
        res.tool_calls = [{"name": n, "args": {}, "is_error": False, "result_preview": ""} for n in old["tool_calls"]]
        res.cli_cost_usd = old["cost_usd"]
        res.latency_s = old["latency_s"]
        res.turns = old["turns"]
        # run-fejl fra den oprindelige kørsel bevares
        orig_err = next((f for f in old["failures"] if f.startswith("run error")), None)
        if orig_err:
            res.error = orig_err.removeprefix("run error: ")
        stored = old.get("judge")
        judge = (lambda q, a, s=stored: s if s else {"comment": "judge_error"})
        row = score_case(case, res, judge)
        if stored is None and case["category"] == "tone":
            row["failures"] = ["judge_error: dommeren svarede ikke i JSON (kør evals igen for denne case)"]
            row["passed"] = False
        # tool-args kendes ikke i rapporten; behold den oprindelige vurdering af grænser
        if any("tool-args uden for grænser" in f for f in old["failures"]):
            row["failures"].append("tool-args uden for grænser (fra oprindelig kørsel)")
            row["passed"] = False
        rows.append(row)
    meta = {k: data["summary"][k] for k in ("model", "prompt_version", "effort", "backend", "ran_at", "wall_s") if k in data["summary"]}
    meta["rescored"] = True
    write_report(label, meta, rows)
    s = json.loads(path.read_text())["summary"]
    print(f"{label}: {s['total_passed']}/{s['total']}  " + "  ".join(f"{c}={v['passed']}/{v['total']}" for c, v in s["per_category"].items()))


if __name__ == "__main__":
    labels = sys.argv[1:] or [p.stem for p in sorted(REPORTS.glob("*.json"))]
    for l in labels:
        rescore(l)
