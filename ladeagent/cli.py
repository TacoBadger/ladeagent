"""CLI til demo og drift.

  python -m ladeagent.cli ask "Hvornår skal jeg lade i nat? København, 40 kWh, 11 kW"
  python -m ladeagent.cli ask --live "..."          # live data i stedet for snapshot
  python -m ladeagent.cli plans                     # vis ladeplaner
  python -m ladeagent.cli approve plan_xxxxxxxx     # menneskelig godkendelse (det eneste sted en plan kan godkendes)
"""
from __future__ import annotations

import argparse
import json
import sys

from ladeagent import config, plans
from ladeagent.agent import Agent
from ladeagent.data.eds import DataStore


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ladeagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ask", help="stil et spørgsmål til agenten")
    a.add_argument("question")
    a.add_argument("--live", action="store_true", help="hent live data fra Energi Data Service")
    a.add_argument("--model", default=config.DEFAULT_MODEL)
    a.add_argument("--prompt", default="v2")
    a.add_argument("--json", action="store_true", help="print hele det strukturerede svar")
    a.add_argument("--backend", choices=["api", "claude-cli"], default="api", help="claude-cli = via Claude Code-abonnement, ingen API-nøgle")
    sub.add_parser("plans", help="vis alle ladeplaner")
    ap_ = sub.add_parser("approve", help="godkend en ladeplan (menneskelig handling)")
    ap_.add_argument("plan_id")
    args = ap.parse_args(argv)

    if args.cmd == "ask":
        store = DataStore.live() if args.live else DataStore.from_snapshot()
        if args.backend == "claude-cli":
            from ladeagent.cli_backend import ClaudeCliAgent
            agent = ClaudeCliAgent(store, model=args.model, prompt_version=args.prompt)
        else:
            agent = Agent(store, model=args.model, prompt_version=args.prompt)
        res = agent.ask(args.question)
        if res.error:
            print(f"FEJL: {res.error}", file=sys.stderr)
        if args.json:
            print(json.dumps(res.to_trace(), ensure_ascii=False, indent=2))
        else:
            print((res.parsed or {}).get("answer") or res.raw_text)
            print(f"\n[{res.model} · {res.prompt_version} · tools: {', '.join(t['name'] for t in res.tool_calls) or 'ingen'} · "
                  f"{res.usage.input_tokens + res.usage.cache_read}+{res.usage.output_tokens} tokens · ${res.cost_usd:.4f} · {res.latency_s:.1f}s · {store.source}]")
        return 1 if res.error else 0
    if args.cmd == "plans":
        print(json.dumps(plans._load(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "approve":
        print(json.dumps(plans.approve_plan(args.plan_id), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
