"""Kundeservice-agenten: Claude + tools + struktureret svar + omkostning + trace.

Manuelt tool-loop (ikke SDK'ens tool runner) fordi vi vil have fuld kontrol
over: hvilke tools der må kaldes, retries mod tool-fejl, tokens og pris pr.
samtale, og et JSONL-trace pr. kørsel som evals og README bygger på.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from ladeagent import config, plans, tools
from ladeagent.data.eds import DataStore

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "evals" / "prompts"

# Struktureret slutsvar. Evals scorer på felterne, ikke på fritekst.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "description": "Svaret til kunden på dansk, kort og konkret."},
        "answer_type": {"type": "string", "enum": ["data", "no_data", "refused", "plan_pending"],
                         "description": "data = svar bygget på tool-data; no_data = spørgsmålet kan ikke besvares med de tilgængelige tools; refused = anmodning der bryder reglerne; plan_pending = et planforslag er oprettet."},
        "area": {"type": ["string", "null"], "enum": ["DK1", "DK2", None]},
        "window_start": {"type": ["string", "null"], "description": "ISO-8601 lokal tid for anbefalet/omtalt vindue, ellers null."},
        "window_end": {"type": ["string", "null"]},
        "cost_dkk": {"type": ["number", "null"], "description": "Det centrale beløb i svaret i DKK (spot), ellers null."},
        "savings_dkk": {"type": ["number", "null"]},
        "plan_id": {"type": ["string", "null"]},
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "answer_type", "area", "window_start", "window_end", "cost_dkk", "savings_dkk", "plan_id", "caveats"],
    "additionalProperties": False,
}


def load_prompt(version: str) -> str:
    return (PROMPTS_DIR / f"{version}.md").read_text()


def build_tools(allow_write: bool = True) -> list[dict]:
    defs = []
    for name, spec in tools.READ_TOOLS.items():
        defs.append({"name": name, "description": spec["description"], "input_schema": spec["schema"], "strict": True})
    if allow_write:
        for name, spec in plans.WRITE_TOOLS.items():
            defs.append({"name": name, "description": spec["description"], "input_schema": spec["schema"], "strict": True})
    return defs


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, u) -> None:
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

    def cost_usd(self, model: str) -> float:
        inp, out = config.price_for(model)
        return (self.input_tokens * inp + self.cache_read * inp * 0.1 + self.cache_write * inp * 1.25 + self.output_tokens * out) / 1e6


@dataclass
class RunResult:
    run_id: str
    question: str
    model: str
    prompt_version: str
    parsed: dict | None
    raw_text: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    turns: int = 0
    latency_s: float = 0.0
    error: str | None = None
    stop_reason: str | None = None
    cli_cost_usd: float | None = None  # sat af cli_backend (Claude Code rapporterer selv prisen)

    @property
    def cost_usd(self) -> float:
        if self.cli_cost_usd is not None:
            return self.cli_cost_usd
        return self.usage.cost_usd(self.model)

    def to_trace(self) -> dict:
        return {
            "run_id": self.run_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": self.model,
            "prompt_version": self.prompt_version, "question": self.question, "parsed": self.parsed,
            "raw_text": self.raw_text, "tool_calls": self.tool_calls, "turns": self.turns,
            "usage": self.usage.__dict__, "cost_usd": round(self.cost_usd, 6),
            "cost_dkk": round(self.cost_usd * config.USD_TO_DKK, 4), "latency_s": round(self.latency_s, 2),
            "stop_reason": self.stop_reason, "error": self.error,
        }


class Agent:
    def __init__(self, store: DataStore, model: str = config.DEFAULT_MODEL, prompt_version: str = "v2",
                 allow_write: bool = True, today: str | None = None, effort: str = "medium",
                 trace_file: Path | None = None, client: anthropic.Anthropic | None = None):
        self.store = store
        self.model = model
        self.prompt_version = prompt_version
        self.allow_write = allow_write
        self.effort = effort
        self.today = today or (config.SNAPSHOT_TODAY if store.source == "snapshot" else time.strftime("%Y-%m-%d"))
        self.client = client or anthropic.Anthropic()
        self.tool_defs = build_tools(allow_write)
        self.trace_file = trace_file or (config.TRACE_DIR / f"{time.strftime('%Y%m%d')}.jsonl")

    # Det stabile system-prompt først (cache-venligt), dato og dækning til sidst.
    def system(self) -> list[dict]:
        cov = self.store.coverage()
        dyn = (
            f"\n\n## Kontekst for denne samtale\n- Dags dato: {self.today}. Brug KUN denne dato som \"i dag\". \"I morgen\" er dagen efter {self.today}.\n"
            f"- Prisdata findes for {cov['prices']['from']} til {cov['prices']['to']} (lokal tid)\n"
            f"- CO2-prognose findes for {cov['co2']['from']} til {cov['co2']['to']}\n"
            f"- Produktionsmix findes for {cov['mix']['from']} til {cov['mix']['to']}\n"
        )
        return [
            {"type": "text", "text": load_prompt(self.prompt_version), "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": dyn},
        ]

    def _execute(self, name: str, args: dict) -> tuple[str, bool]:
        try:
            if name in tools.READ_TOOLS:
                return json.dumps(tools.READ_TOOLS[name]["fn"](self.store, **args), ensure_ascii=False), False
            if self.allow_write and name in plans.WRITE_TOOLS:
                return json.dumps(plans.WRITE_TOOLS[name]["fn"](**args), ensure_ascii=False), False
            return json.dumps({"error": f"Tool {name!r} er ikke tilladt."}), True
        except tools.ToolError as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False), True
        except Exception as e:  # noqa: BLE001  -- aldrig en stack trace til modellen
            return json.dumps({"error": f"Intern fejl i {name}: {type(e).__name__}"}), True

    def ask(self, question: str, max_turns: int = 8) -> RunResult:
        res = RunResult(run_id=uuid.uuid4().hex[:10], question=question, model=self.model, prompt_version=self.prompt_version, parsed=None, raw_text="")
        messages: list[dict] = [{"role": "user", "content": question}]
        t0 = time.time()
        try:
            for _ in range(max_turns):
                resp = self.client.messages.create(
                    model=self.model, max_tokens=4096, system=self.system(), tools=self.tool_defs, messages=messages,
                    output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}},
                )
                res.turns += 1
                res.usage.add(resp.usage)
                res.stop_reason = resp.stop_reason
                if resp.stop_reason == "refusal":
                    res.error = "model refusal"
                    break
                tool_uses = [b for b in resp.content if b.type == "tool_use"]
                if resp.stop_reason != "tool_use" or not tool_uses:
                    res.raw_text = next((b.text for b in resp.content if b.type == "text"), "")
                    break
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for tu in tool_uses:
                    args = tu.input if isinstance(tu.input, dict) else json.loads(tu.input)
                    out, is_err = self._execute(tu.name, args)
                    res.tool_calls.append({"name": tu.name, "args": args, "is_error": is_err, "result_preview": out[:300]})
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": is_err})
                messages.append({"role": "user", "content": results})
            else:
                res.error = f"max_turns={max_turns} nået"
        except anthropic.APIError as e:
            res.error = f"{type(e).__name__}: {getattr(e, 'message', str(e))}"
        res.latency_s = time.time() - t0
        if res.raw_text:
            try:
                res.parsed = json.loads(res.raw_text)
            except json.JSONDecodeError:
                res.error = (res.error or "") + " | svar var ikke gyldig JSON"
        self._trace(res)
        return res

    def _trace(self, res: RunResult) -> None:
        self.trace_file.parent.mkdir(parents=True, exist_ok=True)
        with self.trace_file.open("a") as f:
            f.write(json.dumps(res.to_trace(), ensure_ascii=False) + "\n")
