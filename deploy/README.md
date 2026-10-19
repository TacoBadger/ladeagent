# Hosting: https://in10x.com/ladeagent/mcp (én URL, ingen installation for den der tester)

Ét script gør alt på serveren (clone/pull, venv, tests, systemd-service, forside, nginx-site, TLS via certbot når DNS er på plads). Idempotent.

```bash
ssh hetzner 'curl -sL https://raw.githubusercontent.com/TacoBadger/ladeagent/main/deploy/install.sh | bash'
```

Forudsætning: in10x.com peger på serveren. Ladeagent ligger under `/ladeagent/` på in10x.com-sitet; nginx-blokken (location /ladeagent/ med stiomskrivning via sub_filter + proxy af /ladeagent/mcp og /ladeagent/api/ til 127.0.0.1:8765) vedligeholdes i serverens in10x.com-konfiguration, ikke af install-scriptet.

Manuel test udefra:

```bash
curl -s -X POST https://in10x.com/ladeagent/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Brug det:
- **claude.ai**: Indstillinger → Connectors → Tilføj custom connector → URL `https://in10x.com/ladeagent/mcp`, ingen auth.
- **Claude Desktop**: samme connector-menu.
- **Claude Code**: `claude mcp add --transport http ladeagent https://in10x.com/ladeagent/mcp`

Servicen kører med `--live`, så priserne er dagens rigtige day-ahead-priser (snapshottet bruges kun til evals). Logs: `journalctl -u ladeagent-mcp -f`.

## Forsiden på https://in10x.com/ladeagent/

`site/` er en statisk side uden byggetrin. Scriptet kopierer den til `/var/www/ladeagent`, og nginx serverer den. Tallene i det øverste kort hentes fra `/api/tools/<navn>`, som er de samme read-tools som MCP-serveren bruger (`ladeagent/web.py`). Write-tool'et kan ikke kaldes derfra, og nginx tillader kun GET og højst 5 kald i sekundet pr. adresse. Svarer serveren ikke, viser siden et eksempel fra snapshottet og siger det.

Se siden lokalt med live data:

```bash
.venv/bin/python -m ladeagent.mcp_server --http --live --site   # http://127.0.0.1:8765
```

install-scriptet rører ikke nginx eller TLS længere (siden 2026-10-19). `deploy/nginx-site*.conf` er de gamle skabeloner fra da forsiden lå på eget domæne.
