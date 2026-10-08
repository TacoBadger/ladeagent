"""Kundeservice-agenten: Claude + tools + struktureret svar + omkostning + trace.

Manuelt tool-loop (ikke SDK'ens tool runner) fordi vi vil have fuld kontrol
over: hvilke tools der må kaldes, retries mod tool-fejl, tokens og pris pr.
samtale, et JSONL-trace pr. kørsel som evals og README bygger på, og (runde 2)
hårde lofter pr. samtale, et fuldt trace pr. svar og en audit-linje pr. plan.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from ladeagent import audit, config, plans, tools
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

CAP_REASONS = {
    "cost": f"samtalen nåede loftet på {config.MAX_COST_DKK_PER_CONVERSATION:.0f} kr.",
    "tool_calls": f"samtalen nåede loftet på {config.MAX_TOOL_CALLS_PER_CONVERSATION} opslag",
}


def cap_answer(cap_hit: str) -> dict:
    """Det faste svar kunden får, når et loft stopper samtalen. Ingen tal, ingen gæt."""
    reason = CAP_REASONS.get(cap_hit.split(" ")[0], cap_hit)
    return {"answer": f"Jeg måtte stoppe her, fordi {reason}. Stil gerne spørgsmålet mere konkret (område, tidsrum, kWh og kW), så slår jeg det op igen.",
            "answer_type": "no_data", "area": None, "window_start": None, "window_end": None, "cost_dkk": None,
            "savings_dkk": None, "plan_id": None, "caveats": [f"stoppet af loft: {cap_hit}"]}


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
        """Listepris: samme formel for alle modeller, uanset hvem der har kørt samtalen."""
        inp, out = config.price_for(model)
        rf = config.cache_read_factor(model)
        return (self.input_tokens * inp + self.cache_read * inp * rf + self.cache_write * inp * config.CACHE_WRITE_FACTOR + self.output_tokens * out) / 1e6


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
    cap_hit: str | None = None            # "cost" / "tool_calls" når et loft stoppede samtalen
    backend: str = "api"
    cli_cost_usd: float | None = None     # Claude Codes egen opgørelse (kun info; prisen i rapporter er listeprisen)
    cost_override: float | None = None    # bruges af rescore til at bevare prisen fra en gammel kørsel

    @property
    def trace_id(self) -> str:
        return self.run_id

    @property
    def executed_tool_calls(self) -> list[dict]:
        return [tc for tc in self.tool_calls if tc.get("executed", True)]

    @property
    def cost_usd(self) -> float:
        if self.cost_override is not None:
            return self.cost_override
        return self.usage.cost_usd(self.model)

    @property
    def cost_dkk(self) -> float:
        return self.cost_usd * config.USD_TO_DKK

    def to_trace(self) -> dict:
        return {
            "trace_id": self.trace_id, "run_id": self.run_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "model": self.model,
            "prompt_version": self.prompt_version, "backend": self.backend, "question": self.question, "parsed": self.parsed,
            "raw_text": self.raw_text,
            "tool_calls": [{k: v for k, v in tc.items() if k != "result"} for tc in self.tool_calls], "turns": self.turns,
            "usage": self.usage.__dict__, "cost_usd": round(self.cost_usd, 6), "cost_dkk": round(self.cost_dkk, 4),
            "cli_cost_usd": self.cli_cost_usd, "latency_s": round(self.latency_s, 2),
            "stop_reason": self.stop_reason, "cap_hit": self.cap_hit, "error": self.error,
        }


def _jsonable(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(obj).items()}
    return obj


def finish_run(res: RunResult, *, store: DataStore, trace_file: Path, trace_store: Path | None,
               system_text: str, detail: dict | None = None) -> None:
    """Fælles afslutning for begge backends: kort jsonl-spor, fuldt trace pr. svar, og audit-linje pr. oprettet plan."""
    # 1) audit trail: én linje pr. plan agenten har oprettet i denne samtale
    for tc in res.executed_tool_calls:
        if tc["name"] == "create_charging_plan" and not tc.get("is_error"):
            try:
                plan = json.loads(tc["result"])["plan"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            audit.record_plan(plan=plan, trace_id=res.trace_id, model=res.model, prompt_version=res.prompt_version,
                              backend=res.backend, question=res.question, tool_calls=res.executed_tool_calls,
                              cost_usd=res.cost_usd, data_sha256=store.sha256())
    # 2) kort spor (som hidtil)
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    with trace_file.open("a") as f:
        f.write(json.dumps(res.to_trace(), ensure_ascii=False) + "\n")
    # 3) fuldt trace pr. samtale, ét trace-id pr. svar
    if trace_store is not None:
        trace_store.mkdir(parents=True, exist_ok=True)
        full = res.to_trace()
        full["tool_calls"] = _jsonable(res.tool_calls)
        full["system_sha256"] = hashlib.sha256(system_text.encode()).hexdigest()
        full["data_sha256"] = store.sha256()
        full["data_source"] = store.source
        full["caps"] = {"max_cost_dkk": config.MAX_COST_DKK_PER_CONVERSATION, "max_tool_calls": config.MAX_TOOL_CALLS_PER_CONVERSATION}
        full.update(_jsonable(detail or {}))
        (trace_store / f"{res.trace_id}.json").write_text(json.dumps(full, ensure_ascii=False, indent=1, default=str))


class Agent:
    def __init__(self, store: DataStore, model: str = config.DEFAULT_MODEL, prompt_version: str = "v2",
                 allow_write: bool = True, today: str | None = None, effort: str = "medium",
                 trace_file: Path | None = None, client: anthropic.Anthropic | None = None,
                 trace_store: Path | None = None):
        self.store = store
        self.model = model
        self.prompt_version = prompt_version
        self.allow_write = allow_write
        self.effort = effort
        self.today = today or (config.SNAPSHOT_TODAY if store.source == "snapshot" else time.strftime("%Y-%m-%d"))
        self.client = client or anthropic.Anthropic()
        self.tool_defs = build_tools(allow_write)
        self.trace_file = trace_file or (config.TRACE_DIR / f"{time.strftime('%Y%m%d')}.jsonl")
        self.trace_store = trace_store

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
        cap_calls = config.MAX_TOOL_CALLS_PER_CONVERSATION
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
                # Loft 1: pris. Tjekkes efter hvert modelsvar, før noget mere sker.
                if res.cost_dkk > config.MAX_COST_DKK_PER_CONVERSATION:
                    res.cap_hit = "cost"
                    break
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
                    # Loft 2: antal tool-kald. Kald ud over loftet udføres ikke.
                    if len(res.executed_tool_calls) >= cap_calls:
                        out, is_err, executed = json.dumps({"error": f"Loft nået: højst {cap_calls} tool-kald pr. samtale. Kaldet blev ikke udført."}, ensure_ascii=False), True, False
                    else:
                        (out, is_err), executed = self._execute(tu.name, args), True
                    res.tool_calls.append({"name": tu.name, "args": args, "is_error": is_err, "executed": executed, "result_preview": out[:300], "result": out})
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": is_err})
                messages.append({"role": "user", "content": results})
                if len(res.executed_tool_calls) >= cap_calls:
                    res.cap_hit = "tool_calls"
                    break
            else:
                res.error = f"max_turns={max_turns} nået"
        except anthropic.APIError as e:
            res.error = f"{type(e).__name__}: {getattr(e, 'message', str(e))}"
        res.latency_s = time.time() - t0
        if res.cap_hit:
            res.parsed = cap_answer(res.cap_hit)
            res.raw_text = json.dumps(res.parsed, ensure_ascii=False)
            res.error = f"loft nået: {res.cap_hit}"
        elif res.raw_text:
            try:
                res.parsed = json.loads(res.raw_text)
            except json.JSONDecodeError:
                res.error = (res.error or "") + " | svar var ikke gyldig JSON"
        finish_run(res, store=self.store, trace_file=self.trace_file, trace_store=self.trace_store,
                   system_text="".join(b["text"] for b in self.system()), detail={"messages": messages, "effort": self.effort})
        return res
