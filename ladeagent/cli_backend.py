"""Alternativ backend: kør agenten gennem Claude Code (headless `claude -p`) i stedet for API'et.

Samme tools (via MCP-serveren), samme systemprompt, samme JSON-skema for svaret,
samme RunResult, samme lofter, samme trace og samme audit-linje. Forskellen er kun
hvem der betaler: et Claude-abonnement i stedet for API-tokens. Bruges af
`make eval-cli`, `make ask-cli` og `make quality`.

Lofter i denne backend:
  - tool-kald: `--max-turns` = config.MAX_TOOL_CALLS_PER_CONVERSATION. Claude Code stopper selv.
  - pris: `--max-budget-usd` = loftet i USD, når Claude Code kender modellens pris (costBasis != unknown).
    Kender den ikke prisen (nye modeller prises som Opus 5 af CLI'en), håndhæves loftet efter
    samtalen på listeprisen: svaret erstattes af den faste stop-besked. Hvilken af de to der
    gjaldt, står i tracet som cost_cap_mode.
Prisen i rapporter er altid listeprisen (tokens x config.PRICES_USD_PER_MTOK), ikke CLI'ens tal.

Kræver at `claude` er installeret og logget ind i DEN terminal, kommandoen køres fra.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ladeagent import config
from ladeagent.agent import ANSWER_SCHEMA, RunResult, Usage, cap_answer, finish_run, load_prompt
from ladeagent.data.eds import DataStore

ROOT = Path(__file__).resolve().parent.parent
MCP_CONFIG = ROOT / "mcp.json"
TOOL_PREFIX = "mcp__ladeagent__"
BUILTIN_TOOLS = "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,WebSearch,WebFetch,Task,TodoWrite,NotebookEdit,LS"

_CLI_KNOWS_PRICE: dict[str, bool] = {}
HOOK_SCRIPT = Path(__file__).resolve().parent / "tool_cap_hook.py"
CAP_MESSAGE = "Loft nået"


def hook_settings() -> str:
    """Claude Code-settings med PreToolUse-hooken, der håndhæver loftet på tool-kald pr. samtale (samme tal som agent.py)."""
    return json.dumps({"hooks": {"PreToolUse": [{"matcher": f"{TOOL_PREFIX}.*",
                                                 "hooks": [{"type": "command", "command": f"{sys.executable} {HOOK_SCRIPT}", "timeout": 15}]}]}})


def unwrap_tool_result(content) -> str:
    """Claude Code pakker et MCP-tekstsvar ind som {"result": "<tekst>"}. Vi vil have teksten (tool'ets egen JSON)."""
    if isinstance(content, list):  # [{"type":"text","text":"..."}]
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(d, dict) and set(d) == {"result"} and isinstance(d["result"], str):
        return d["result"]
    return text


def system_text(store: DataStore, prompt_version: str, today: str) -> str:
    cov = store.coverage()
    return (
        load_prompt(prompt_version)
        + f"\n\n## Kontekst for denne samtale\n- Dags dato: {today}. Brug KUN denne dato som \"i dag\", også hvis du ser en anden dato i systemoplysninger. \"I morgen\" er dagen efter {today}.\n"
        f"- Prisdata findes for {cov['prices']['from']} til {cov['prices']['to']} (lokal tid)\n"
        f"- CO2-prognose findes for {cov['co2']['from']} til {cov['co2']['to']}\n"
        f"- Produktionsmix findes for {cov['mix']['from']} til {cov['mix']['to']}\n"
        "- Værktøjerne hedder mcp__ladeagent__<navn>. Brug dem; du har ingen andre værktøjer.\n"
    )


def claude_cli(args: list[str], timeout: int = 180) -> tuple[list[dict], str]:
    """Kører `claude -p ... --output-format stream-json` og returnerer alle events + rå stderr."""
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("`claude` (Claude Code CLI) er ikke installeret eller ikke på PATH.")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}  # tillad kald inde fra en Claude Code-session
    proc = subprocess.run([exe, *args], cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events, proc.stderr


def cli_knows_price(model: str) -> bool:
    """Spørger Claude Code én gang pr. proces, om den kender modellens pris. Afgør om --max-budget-usd kan bruges."""
    if model not in _CLI_KNOWS_PRICE:
        known = False
        try:
            events, _ = claude_cli(["-p", "Svar kun med ordet OK", "--output-format", "stream-json", "--verbose", "--tools", "",
                                    "--model", model, "--max-turns", "1", "--no-session-persistence"], timeout=90)
            result = next((ev for ev in events if ev.get("type") == "result"), {})
            mu = (result.get("modelUsage") or {}).get(model) or {}
            known = bool(mu) and mu.get("costBasis") != "unknown"
        except (RuntimeError, subprocess.TimeoutExpired):
            known = False
        _CLI_KNOWS_PRICE[model] = known
    return _CLI_KNOWS_PRICE[model]


class ClaudeCliAgent:
    def __init__(self, store: DataStore, model: str = config.DEFAULT_MODEL, prompt_version: str = "v2",
                 today: str | None = None, trace_file: Path | None = None, trace_store: Path | None = None):
        self.store = store
        self.model = model
        self.prompt_version = prompt_version
        self.today = today or (config.SNAPSHOT_TODAY if store.source == "snapshot" else time.strftime("%Y-%m-%d"))
        self.trace_file = trace_file or (config.TRACE_DIR / f"cli_{time.strftime('%Y%m%d')}.jsonl")
        self.trace_store = trace_store
        self.budget_flag = cli_knows_price(model)

    def ask(self, question: str, max_turns: int | None = None) -> RunResult:
        max_turns = max_turns or config.MAX_TOOL_CALLS_PER_CONVERSATION
        res = RunResult(run_id=uuid.uuid4().hex[:10], question=question, model=self.model, prompt_version=self.prompt_version,
                        parsed=None, raw_text="", backend="claude-cli")
        sys_text = system_text(self.store, self.prompt_version, self.today)
        cap_usd = config.MAX_COST_DKK_PER_CONVERSATION / config.USD_TO_DKK
        t0 = time.time()
        args = [
            "-p", question, "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(ANSWER_SCHEMA),
            "--mcp-config", str(MCP_CONFIG), "--strict-mcp-config",
            "--system-prompt", sys_text,
            "--allowedTools", f"{TOOL_PREFIX}*", "--disallowedTools", BUILTIN_TOOLS,
            "--model", self.model, "--max-turns", str(max_turns), "--no-session-persistence",
        ]
        args += ["--settings", hook_settings()]  # loft på tool-kald, håndhævet pr. kald af tool_cap_hook.py
        if self.budget_flag:
            args += ["--max-budget-usd", f"{cap_usd:.4f}"]
        # Rate limit (abonnementets forbrugsgrænse) giver tomme svar. Det er ikke modellens fejl:
        # vent og prøv igen op til to gange, og notér det i sporet.
        rate_limited = 0
        events: list[dict] = []
        stderr = ""
        for attempt in range(3):
            try:
                events, stderr = claude_cli(args)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                res.error = str(e)
                res.latency_s = time.time() - t0
                finish_run(res, store=self.store, trace_file=self.trace_file, trace_store=self.trace_store, system_text=sys_text,
                           detail={"events": events, "cost_cap_mode": "claude-cli budget" if self.budget_flag else "post hoc (listepris)"})
                return res
            rl = sum(1 for ev in events if ev.get("type") == "rate_limit_event")
            result_ev = next((ev for ev in events if ev.get("type") == "result"), None)
            empty = result_ev is None or (not isinstance(result_ev.get("structured_output"), dict) and not (result_ev.get("result") or "").strip())
            capped = bool(result_ev) and str(result_ev.get("subtype", "")).startswith("error_max_")
            if rl and empty and not capped and attempt < 2:
                rate_limited += 1
                time.sleep(20 * (attempt + 1))
                continue
            break
        res.rate_limited = rate_limited
        result = None
        by_id: dict[str, dict] = {}
        for ev in events:
            if ev.get("type") == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        if not b["name"].startswith(TOOL_PREFIX):
                            continue  # Claude Codes interne værktøjer (ToolSearch, StructuredOutput) er ikke agentens tools
                        tc = {"name": b["name"].removeprefix(TOOL_PREFIX), "args": b.get("input", {}), "is_error": False,
                              "executed": True, "result_preview": "", "result": ""}
                        res.tool_calls.append(tc)
                        by_id[b.get("id", "")] = tc
                res.turns += 1
            elif ev.get("type") == "user":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_result" and b.get("tool_use_id") in by_id:
                        text = unwrap_tool_result(b.get("content"))
                        tc = by_id[b["tool_use_id"]]
                        tc["result"] = text
                        tc["result_preview"] = text[:300]
                        tc["is_error"] = bool(b.get("is_error")) or '"error"' in text[:40]
                        if CAP_MESSAGE in text[:200]:
                            tc["executed"] = False  # blokeret af hooken: loftet på tool-kald
            elif ev.get("type") == "result":
                result = ev
        res.latency_s = time.time() - t0
        if result is None:
            res.error = "intet result-event fra claude" + (f": {stderr.strip()[:200]}" if stderr.strip() else "")
        else:
            u = result.get("usage", {})
            res.usage = Usage(input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                              cache_read=u.get("cache_read_input_tokens", 0), cache_write=u.get("cache_creation_input_tokens", 0))
            res.stop_reason = result.get("stop_reason")
            res.cli_cost_usd = float(result.get("total_cost_usd") or 0.0)
            subtype = str(result.get("subtype", ""))
            from ladeagent.tool_cap_hook import counter_path
            counter_path(str(result.get("session_id", "nosession"))).unlink(missing_ok=True)
            if subtype == "error_max_budget_usd":
                res.cap_hit = "cost"
            elif subtype == "error_max_turns" or any(not tc["executed"] for tc in res.tool_calls):
                res.cap_hit = "tool_calls"
            elif result.get("is_error"):
                res.error = str(result.get("result") or subtype)[:300]
            so = result.get("structured_output")
            if isinstance(so, dict):
                res.parsed = so
                res.raw_text = json.dumps(so, ensure_ascii=False)
            else:
                res.raw_text = str(result.get("result") or "")
                if res.raw_text and res.cap_hit is None:
                    try:
                        res.parsed = json.loads(res.raw_text)
                    except json.JSONDecodeError:
                        res.error = (res.error or "") + " | svar var ikke gyldig JSON"
            # Loft håndhævet efter samtalen, når CLI'en ikke kunne gøre det undervejs
            if res.cap_hit is None and res.cost_dkk > config.MAX_COST_DKK_PER_CONVERSATION:
                res.cap_hit = "cost (post hoc)"
            if res.cap_hit:
                res.parsed = cap_answer(res.cap_hit)
                res.raw_text = json.dumps(res.parsed, ensure_ascii=False)
                res.error = f"loft nået: {res.cap_hit}"
        finish_run(res, store=self.store, trace_file=self.trace_file, trace_store=self.trace_store, system_text=sys_text,
                   detail={"events": events, "rate_limited_retries": rate_limited,
                           "cost_cap_mode": "claude-cli budget" if self.budget_flag else "post hoc (listepris)"})
        return res


def judge_cli(question: str, answer: str, schema: dict, model: str = config.JUDGE_MODEL, _retry: bool = True) -> dict:
    """LLM-judge via Claude Code (samme rubrik som API-varianten). Prøver igen én gang, hvis svaret ikke er JSON."""
    out = _judge_cli_once(question, answer, schema, model)
    if _retry and not all(k in out for k in ("danish", "concise", "polite", "caveat_ok", "honest")):
        out = _judge_cli_once(question, answer, schema, model)
    return out


def _judge_cli_once(question: str, answer: str, schema: dict, model: str) -> dict:
    events, stderr = claude_cli([
        "-p", f"Kundens spørgsmål:\n{question}\n\nAssistentens svar:\n{answer}",
        "--output-format", "json", "--json-schema", json.dumps(schema),
        "--system-prompt", "Du bedømmer et kundeservice-svar fra en dansk energiselskabs-assistent efter en fast rubrik. Vær streng men fair.",
        "--tools", "", "--disallowedTools", BUILTIN_TOOLS, "--model", model, "--max-turns", "1", "--no-session-persistence",
    ])
    for ev in events:
        if ev.get("type") == "result":
            so = ev.get("structured_output")
            if isinstance(so, dict):
                return so
            try:
                return json.loads(ev.get("result") or "")
            except json.JSONDecodeError:
                return {"comment": f"judge gav ikke JSON: {str(ev.get('result'))[:100]}"}
    return {"comment": f"judge fejlede: {stderr[:100]}"}
