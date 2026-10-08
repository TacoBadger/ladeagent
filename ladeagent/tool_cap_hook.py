"""PreToolUse-hook til Claude Code: samme loft på tool-kald som i agent.py, håndhævet pr. kald.

Claude Code kalder scriptet før hvert mcp__ladeagent__-kald med JSON på stdin (session_id,
tool_name, ...). Scriptet tæller kald pr. session i en fil i temp-mappen. Kald nr. N+1 blokeres
(exit 2), og beskeden på stderr går tilbage til modellen som tool-fejl. Bruges af cli_backend.py
gennem `claude -p --settings '{"hooks": ...}'`, så CLI-backenden får samme hårde grænse som API-loopet.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from ladeagent import config

PREFIX = "mcp__ladeagent__"


def counter_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"ladeagent_toolcap_{session_id}"


def main() -> int:
    try:
        d = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if not str(d.get("tool_name", "")).startswith(PREFIX):
        return 0
    cap = config.MAX_TOOL_CALLS_PER_CONVERSATION
    p = counter_path(str(d.get("session_id", "nosession")))
    n = int(p.read_text()) if p.exists() else 0
    if n >= cap:
        print(f"Loft nået: højst {cap} tool-kald pr. samtale. Kaldet blev ikke udført. "
              "Afslut med det du allerede har, og bed kunden spørge mere konkret.", file=sys.stderr)
        return 2
    p.write_text(str(n + 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
