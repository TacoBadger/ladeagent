.PHONY: setup snapshot golden test mcp inspector ask ask-cli eval eval-cli eval-all eval-all-cli report quality quality-all quality-merge clean
PY=.venv/bin/python
PROMPT?=v2
MODEL?=claude-opus-5
Q?=Hvornår skal jeg lade i nat? Jeg bor i København, 40 kWh, 11 kW ladeboks.
RUN?=1
BACKEND?=claude-cli

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

quality:          ## runde 2: alle seks tests for én model og kørsel: make quality MODEL=claude-sonnet-5 RUN=1
	$(PY) -m evals.quality --prompt $(PROMPT) --model $(MODEL) --run $(RUN) --backend $(BACKEND)

quality-all:      ## runde 2: v1-beviset + baseline og tre kandidater, to kørsler hver (9 x 30 spørgsmål)
	$(PY) -m evals.quality --prompt v1 --model claude-sonnet-5 --run 1 --backend $(BACKEND) --expect fail
	for m in claude-sonnet-5 claude-sonnet-5-5 claude-haiku-5-5 claude-fable-5-1; do for r in 1 2; do \
	  $(PY) -m evals.quality --prompt v2 --model $$m --run $$r --backend $(BACKEND) || exit 1; done; done

quality-merge:    ## saml evals/reports/quality/*.json til quality.json og vis dommen
	$(PY) -m evals.quality --merge

clean:
	rm -rf data/cache traces/*.jsonl .pytest_cache
