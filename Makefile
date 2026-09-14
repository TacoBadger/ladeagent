.PHONY: setup snapshot golden test mcp inspector ask ask-cli eval eval-cli eval-all eval-all-cli report clean
PY=.venv/bin/python
PROMPT?=v2
MODEL?=claude-opus-5
Q?=Hvornår skal jeg lade i nat? Jeg bor i København, 40 kWh, 11 kW ladeboks.

setup:            ## venv + afhængigheder + pakke
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt pytest-asyncio && .venv/bin/pip install -q -e .

snapshot:         ## frys data fra Energi Data Service til data/snapshot/
	$(PY) scripts/fetch_snapshot.py

golden:           ## regn facit til evals fra snapshottet
	$(PY) -m evals.make_golden

test:             ## deterministiske tests (ingen model, ingen netværk)
	$(PY) -m pytest -q

mcp:              ## kør MCP-serveren (stdio)
	$(PY) -m ladeagent.mcp_server

inspector:        ## åbn MCP Inspector mod serveren
	npx -y @modelcontextprotocol/inspector $(PY) -m ladeagent.mcp_server

ask:              ## spørg agenten: make ask Q="..."
	$(PY) -m ladeagent.cli ask "$(Q)" --model $(MODEL) --prompt $(PROMPT)

eval:             ## kør evals: make eval PROMPT=v2 MODEL=claude-opus-5
	$(PY) -m evals.run_evals --prompt $(PROMPT) --model $(MODEL)

eval-all:         ## de fire kørsler README'en sammenligner
	$(PY) -m evals.run_evals --prompt v1 --model claude-opus-5
	$(PY) -m evals.run_evals --prompt v2 --model claude-opus-5
	$(PY) -m evals.run_evals --prompt v2 --model claude-sonnet-5
	$(PY) -m evals.run_evals --prompt v2 --model claude-haiku-4-5

ask-cli:          ## spørg agenten via Claude Code-abonnement (ingen API-nøgle): make ask-cli Q="..."
	$(PY) -m ladeagent.cli ask "$(Q)" --model $(MODEL) --prompt $(PROMPT) --backend claude-cli

eval-cli:         ## evals via Claude Code-abonnement: make eval-cli PROMPT=v2 MODEL=claude-opus-5
	$(PY) -m evals.run_evals --prompt $(PROMPT) --model $(MODEL) --backend claude-cli

eval-all-cli:     ## de fire kørsler, via Claude Code-abonnement
	$(PY) -m evals.run_evals --prompt v1 --model claude-opus-5 --backend claude-cli
	$(PY) -m evals.run_evals --prompt v2 --model claude-opus-5 --backend claude-cli
	$(PY) -m evals.run_evals --prompt v2 --model claude-sonnet-5 --backend claude-cli
	$(PY) -m evals.run_evals --prompt v2 --model claude-haiku-4-5 --backend claude-cli

report:           ## vis sammenligningen
	@cat evals/reports/summary.md

clean:
	rm -rf data/cache traces/*.jsonl .pytest_cache
