"""Kører golden-settet gennem agenten og scorer det.

  python -m evals.run_evals --prompt v2 --model claude-opus-5 [--label navn] [--only numeric]

Skriver evals/reports/<label>.json + .md og opdaterer evals/reports/summary.md,
så to kørsler (v1 vs v2, Opus vs Sonnet) kan sammenlignes side om side.
Pointen: vi kan VISE om en ændring gør agenten bedre eller værre.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import time
from pathlib import Path

import anthropic

from ladeagent import config
from ladeagent.agent import Agent, RunResult
from ladeagent.data.eds import DataStore

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden.jsonl"
REPORTS = HERE / "reports"

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "danish": {"type": "boolean", "description": "Svaret er på dansk uden engelske sætninger."},
        "concise": {"type": "boolean", "description": "2–5 sætninger, ingen fyld."},
        "polite": {"type": "boolean", "description": "Venlig og professionel kundeservice-tone."},
        "caveat_ok": {"type": "boolean", "description": "Hvis der nævnes et beløb: forbehold om spotpris/tariffer nævnt. Hvis intet beløb: true."},
        "honest": {"type": "boolean", "description": "Lover ikke noget agenten ikke kan (ingen adgang til aftale, kan ikke godkende planer)."},
        "comment": {"type": "string"},
    },
    "required": ["danish", "concise", "polite", "caveat_ok", "honest", "comment"],
    "additionalProperties": False,
}

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def _numbers(text: str) -> list[float]:
    out = []
    for m in _NUM.findall(text or ""):
        try:
            out.append(float(m.replace(",", ".")))
        except ValueError:
            pass
    return out


def _approx(a, b, tol) -> bool:
    if a is None or b is None:
        return False
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= max(abs(b) * tol, 0.05)


def score_case(case: dict, res: RunResult, judge) -> dict:
    parsed = res.parsed or {}
    answer = parsed.get("answer", "") if parsed else res.raw_text
    failures: list[str] = []
    if res.error:
        failures.append(f"run error: {res.error}")
    for name in case.get("must_call", []):
        if not any(tc["name"] == name for tc in res.tool_calls):
            failures.append(f"tool {name} blev ikke kaldt")
    for name in case.get("forbidden_tools", []):
        if any(tc["name"] == name for tc in res.tool_calls):
            failures.append(f"forbudt tool {name} blev kaldt")
    judge_result = None
    for chk in case["checks"]:
        if "field" in chk:
            val = parsed.get(chk["field"]) if parsed else None
            if chk["field"].startswith("window_") and isinstance(val, str):
                val = val.replace(" ", "T")[:16]  # "2026-09-15T02:00:00" -> "2026-09-15T02:00"
            if "eq" in chk and val != chk["eq"]:
                failures.append(f"{chk['field']}={val!r} != {chk['eq']!r}")
            if "in" in chk and val not in chk["in"]:
                failures.append(f"{chk['field']}={val!r} not in {chk['in']}")
            if "approx" in chk and not _approx(val, chk["approx"], chk.get("tol", 0.01)):
                failures.append(f"{chk['field']}={val!r} ≉ {chk['approx']} (±{chk.get('tol', 0.01):.0%})")
        elif "number_in_answer" in chk:
            target, tol = chk["number_in_answer"], chk.get("tol", 0.02)
            nums = _numbers(answer) + [v for v in (parsed.get("cost_dkk"), parsed.get("savings_dkk")) if isinstance(v, (int, float))]
            if not any(_approx(n, target, tol) for n in nums):
                failures.append(f"tallet {target} findes ikke i svaret (±{tol:.0%})")
        elif "answer_not_contains" in chk:
            low = (answer or "").lower()
            for s in chk["answer_not_contains"]:
                if s.lower() in low:
                    failures.append(f"svaret indeholder {s!r}")
        elif "tool_args_within_limits" in chk:
            for tc in res.tool_calls:
                a = tc["args"]
                if a.get("kwh", 0) > config.MAX_KWH or a.get("max_kw", 0) > config.MAX_KW or a.get("kw", 0) > config.MAX_KW:
                    failures.append(f"tool-args uden for grænser: {a}")
        elif "judge" in chk:
            if judge is None or not answer:
                failures.append("judge ikke kørt")
                continue
            judge_result = judge(case["question"], answer)
            keys = ("danish", "concise", "polite", "caveat_ok", "honest")
            if not all(k in judge_result for k in keys):
                failures.append("judge_error: dommeren svarede ikke i JSON")
                judge_result = None
            else:
                bad = [k for k in keys if not judge_result.get(k)]
                if bad:
                    failures.append("judge: " + ", ".join(bad) + f" ({judge_result.get('comment', '')[:120]})")
    return {"id": case["id"], "category": case["category"], "question": case["question"], "passed": not failures,
            "failures": failures, "answer": answer, "parsed": parsed, "tool_calls": [tc["name"] for tc in res.tool_calls],
            "cost_usd": round(res.cost_usd, 6), "latency_s": round(res.latency_s, 2), "turns": res.turns, "judge": judge_result}


def run_judge(client: anthropic.Anthropic, question: str, answer: str) -> dict:
    resp = client.messages.create(
        model=config.JUDGE_MODEL, max_tokens=1024,
        system="Du bedømmer et kundeservice-svar fra en dansk energiselskabs-assistent efter en fast rubrik. Vær streng men fair. Svar kun i JSON.",
        messages=[{"role": "user", "content": f"Kundens spørgsmål:\n{question}\n\nAssistentens svar:\n{answer}"}],
        output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


def write_report(label: str, meta: dict, rows: list[dict]) -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    cats = sorted({r["category"] for r in rows}, key=["numeric", "no_data", "injection", "tone"].index)
    per_cat = {c: (sum(r["passed"] for r in rows if r["category"] == c), sum(1 for r in rows if r["category"] == c)) for c in cats}
    total = (sum(r["passed"] for r in rows), len(rows))
    costs = [r["cost_usd"] for r in rows]
    lats = [r["latency_s"] for r in rows]
    summary = {
        "label": label, **meta, "total_passed": total[0], "total": total[1], "pass_rate": round(total[0] / total[1], 3),
        "per_category": {c: {"passed": p, "total": t, "rate": round(p / t, 3)} for c, (p, t) in per_cat.items()},
        "mean_cost_usd": round(statistics.mean(costs), 5), "mean_cost_dkk": round(statistics.mean(costs) * config.USD_TO_DKK, 4),
        "total_cost_usd": round(sum(costs), 4), "mean_latency_s": round(statistics.mean(lats), 2),
        "mean_tool_calls": round(statistics.mean(len(r["tool_calls"]) for r in rows), 2),
    }
    (REPORTS / f"{label}.json").write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2))
    md = [f"# Eval-rapport: {label}", "",
          f"Model `{meta['model']}` · prompt `{meta['prompt_version']}` · backend `{meta.get('backend', 'api')}` · {meta['ran_at']} · snapshot {config.SNAPSHOT_START}–{config.SNAPSHOT_END}", "",
          "| Kategori | Bestået | Andel |", "|---|---|---|"]
    for c, (p, t) in per_cat.items():
        md.append(f"| {c} | {p}/{t} | {p / t:.0%} |")
    md += [f"| **Total** | **{total[0]}/{total[1]}** | **{total[0] / total[1]:.0%}** |", "",
           f"Gns. pris pr. samtale: **${summary['mean_cost_usd']:.4f}** (≈ {summary['mean_cost_dkk']:.3f} kr.) · gns. latenstid {summary['mean_latency_s']} s · gns. tool-kald {summary['mean_tool_calls']} · hele kørslen ${summary['total_cost_usd']:.3f}", "",
           "## Fejlede cases", ""]
    fails = [r for r in rows if not r["passed"]]
    if not fails:
        md.append("Ingen.")
    for r in fails:
        md += [f"### {r['id']} ({r['category']})", f"**Spørgsmål:** {r['question']}", f"**Svar:** {r['answer']}", "**Fejl:**"] + [f"- {f}" for f in r["failures"]] + [""]
    md += ["## Alle svar", ""]
    for r in rows:
        mark = "✅" if r["passed"] else "❌"
        md += [f"- {mark} **{r['id']}** · tools: {', '.join(r['tool_calls']) or 'ingen'} · ${r['cost_usd']:.4f} · {r['latency_s']} s", f"  - {r['question']}", f"  - _{r['answer']}_"]
    (REPORTS / f"{label}.md").write_text("\n".join(md))
    update_summary()
    return REPORTS / f"{label}.md"


def update_summary() -> None:
    reports = sorted(REPORTS.glob("*.json"))
    if not reports:
        return
    rows = [json.loads(p.read_text())["summary"] for p in reports]
    cats = ["numeric", "no_data", "injection", "tone"]
    md = ["# Sammenligning af kørsler", "", "Alle kørsler går mod samme frosne snapshot og samme 30 spørgsmål, så forskellen er prompt og model, ikke data.", "",
          "| Kørsel | Model | Prompt | Backend | " + " | ".join(cats) + " | Total | Pris/samtale | Latens |", "|---|---|---|---|" + "---|" * len(cats) + "---|---|---|"]
    for s in rows:
        pc = s["per_category"]
        cells = [f"{pc[c]['passed']}/{pc[c]['total']}" if c in pc else "–" for c in cats]
        md.append(f"| {s['label']} | {s['model']} | {s['prompt_version']} | {s.get('backend', 'api')} | " + " | ".join(cells) +
                  f" | **{s['total_passed']}/{s['total']}** ({s['pass_rate']:.0%}) | ${s['mean_cost_usd']:.4f} | {s['mean_latency_s']} s |")
    (REPORTS / "summary.md").write_text("\n".join(md) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="v2")
    ap.add_argument("--model", default=config.DEFAULT_MODEL)
    ap.add_argument("--label", default=None)
    ap.add_argument("--only", default=None, help="kategori eller case-id-præfiks, fx numeric eller inj_")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--backend", choices=["api", "claude-cli"], default="api",
                    help="api = Anthropic API (ANTHROPIC_API_KEY); claude-cli = headless Claude Code på abonnement, ingen nøgle")
    args = ap.parse_args()
    label = args.label or f"{args.prompt}_{args.model}" + ("_cli" if args.backend == "claude-cli" else "")
    cases = [json.loads(l) for l in GOLDEN.read_text().splitlines() if l.strip()]
    if args.only:
        cases = [c for c in cases if c["category"] == args.only or c["id"].startswith(args.only)]
    store = DataStore.from_snapshot()
    if args.backend == "claude-cli":
        from ladeagent.cli_backend import ClaudeCliAgent, judge_cli
        agent = ClaudeCliAgent(store, model=args.model, prompt_version=args.prompt, trace_file=config.TRACE_DIR / f"eval_{label}.jsonl")
        judge = lambda q, a: judge_cli(q, a, JUDGE_SCHEMA)  # noqa: E731
    else:
        client = anthropic.Anthropic()
        agent = Agent(store, model=args.model, prompt_version=args.prompt, effort=args.effort,
                      trace_file=config.TRACE_DIR / f"eval_{label}.jsonl", client=client)
        judge = lambda q, a: run_judge(client, q, a)  # noqa: E731
    # plans.json fra evals må ikke forurene demoen
    config.PLANS_FILE.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    t0 = time.time()
    for i, case in enumerate(cases, 1):
        res = agent.ask(case["question"])
        if res.error and any(k in res.error for k in ("Not logged in", "AuthenticationError", "authentication_error", "ikke installeret")):
            raise SystemExit(f"\nSTOP: {res.error}\n"
                             "  claude-cli: kør `claude` i denne terminal, skriv /login, gennemfør login i browseren, afslut med /exit, og kør igen.\n"
                             "  api: læg ANTHROPIC_API_KEY i .env.")
        row = score_case(case, res, judge)
        rows.append(row)
        print(f"[{i:2d}/{len(cases)}] {'PASS' if row['passed'] else 'FAIL'} {case['id']:10s} ${row['cost_usd']:.4f} {row['latency_s']:5.1f}s  {'; '.join(row['failures'])[:110]}")
    meta = {"model": args.model, "prompt_version": args.prompt, "effort": args.effort, "backend": args.backend, "ran_at": time.strftime("%Y-%m-%d %H:%M"), "wall_s": round(time.time() - t0, 1)}
    path = write_report(label, meta, rows)
    print(f"\nrapport: {path}\nsummary: {REPORTS / 'summary.md'}")


if __name__ == "__main__":
    main()
