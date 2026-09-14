"""Alternativ backend: kør agenten gennem Claude Code (headless `claude -p`) i stedet for API'et.

Samme tools (via MCP-serveren), samme systemprompt, samme JSON-skema for svaret,
samme RunResult. Forskellen er kun hvem der betaler: et Claude-abonnement i
stedet for API-tokens. Bruges af `make eval-cli` og `make ask-cli`.

Kræver at `claude` er installeret og logget ind i DEN terminal, kommandoen køres fra.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from ladeagent import config
from ladeagent.agent import ANSWER_SCHEMA, RunResult, Usage, load_prompt
from ladeagent.data.eds import DataStore

ROOT = Path(__file__).resolve().parent.parent
MCP_CONFIG = ROOT / "mcp.json"
TOOL_PREFIX = "mcp__ladeagent__"
BUILTIN_TOOLS = "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,WebSearch,WebFetch,Task,TodoWrite,NotebookEdit,LS"


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


class ClaudeCliAgent:
    def __init__(self, store: DataStore, model: str = config.DEFAULT_MODEL, prompt_version: str = "v2",
                 today: str | None = None, trace_file: Path | None = None):
        self.store = store
        self.model = model
        self.prompt_version = prompt_version
        self.today = today or (config.SNAPSHOT_TODAY if store.source == "snapshot" else time.strftime("%Y-%m-%d"))
        self.trace_file = trace_file or (config.TRACE_DIR / f"cli_{time.strftime('%Y%m%d')}.jsonl")

    def ask(self, question: str, max_turns: int = 8) -> RunResult:
        res = RunResult(run_id=uuid.uuid4().hex[:10], question=question, model=self.model, prompt_version=self.prompt_version, parsed=None, raw_text="")
        t0 = time.time()
        args = [
            "-p", question, "--output-format", "stream-json", "--verbose", "--json-schema", json.dumps(ANSWER_SCHEMA),
            "--mcp-config", str(MCP_CONFIG), "--strict-mcp-config",
            "--system-prompt", system_text(self.store, self.prompt_version, self.today),
            "--allowedTools", f"{TOOL_PREFIX}*", "--disallowedTools", BUILTIN_TOOLS,
            "--model", self.model, "--max-turns", str(max_turns), "--no-session-persistence",
        ]
        try:
            events, stderr = claude_cli(args)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            res.error = str(e)
            res.latency_s = time.time() - t0
            self._trace(res)
            return res
        result = None
        for ev in events:
            if ev.get("type") == "assistant":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_use":
                        if not b["name"].startswith(TOOL_PREFIX):
                            continue  # Claude Codes interne værktøjer (ToolSearch, StructuredOutput) er ikke agentens tools
                        name = b["name"].removeprefix(TOOL_PREFIX)
                        res.tool_calls.append({"name": name, "args": b.get("input", {}), "is_error": False, "result_preview": ""})
                res.turns += 1
            elif ev.get("type") == "user":
                for b in ev.get("message", {}).get("content", []):
                    if b.get("type") == "tool_result" and res.tool_calls:
                        content = b.get("content")
                        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                        res.tool_calls[-1]["result_preview"] = text[:300]
                        res.tool_calls[-1]["is_error"] = bool(b.get("is_error")) or '"error"' in text[:40]
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
            if result.get("is_error"):
                res.error = str(result.get("result"))[:300]
            so = result.get("structured_output")
            if isinstance(so, dict):
                res.parsed = so
                res.raw_text = json.dumps(so, ensure_ascii=False)
            else:
                res.raw_text = str(result.get("result") or "")
                try:
                    res.parsed = json.loads(res.raw_text)
                except json.JSONDecodeError:
                    res.error = (res.error or "") + " | svar var ikke gyldig JSON"
        self._trace(res)
        return res

    def _trace(self, res: RunResult) -> None:
        self.trace_file.parent.mkdir(parents=True, exist_ok=True)
        d = res.to_trace()
        d["backend"] = "claude-cli"
        with self.trace_file.open("a") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")


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
