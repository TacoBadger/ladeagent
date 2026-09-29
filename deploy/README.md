# Hosting: https://fontlume.com/mcp (én URL, ingen installation for den der tester)

Ét script gør alt på serveren (clone/pull, venv, tests, systemd-service, forside, nginx-site, TLS via certbot når DNS er på plads). Idempotent.

```bash
ssh hetzner 'curl -sL https://raw.githubusercontent.com/TacoBadger/ladeagent/main/deploy/install.sh | bash'
```

Forudsætning: A-record for `fontlume.com` (@) peger på serverens IP (Hostinger DNS). Vil man beholde roden til noget andet, kør `LADEAGENT_DOMAIN=ladeagent.fontlume.com` foran scriptet og peg det subdomæne i stedet. Scriptet siger selv, hvis DNS ikke er på plads endnu, og kan bare køres igen bagefter.

Manuel test udefra:

```bash
curl -s -X POST https://fontlume.com/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Brug det:
- **claude.ai**: Indstillinger → Connectors → Tilføj custom connector → URL `https://fontlume.com/mcp`, ingen auth.
- **Claude Desktop**: samme connector-menu.
- **Claude Code**: `claude mcp add --transport http ladeagent https://fontlume.com/mcp`

Servicen kører med `--live`, så priserne er dagens rigtige day-ahead-priser (snapshottet bruges kun til evals). Logs: `journalctl -u ladeagent-mcp -f`.

## Forsiden på https://fontlume.com

`site/` er en statisk side uden byggetrin. Scriptet kopierer den til `/var/www/ladeagent`, og nginx serverer den. Tallene i det øverste kort hentes fra `/api/tools/<navn>`, som er de samme read-tools som MCP-serveren bruger (`ladeagent/web.py`). Write-tool'et kan ikke kaldes derfra, og nginx tillader kun GET og højst 5 kald i sekundet pr. adresse. Svarer serveren ikke, viser siden et eksempel fra snapshottet og siger det.

Se siden lokalt med live data:

```bash
.venv/bin/python -m ladeagent.mcp_server --http --live --site   # http://127.0.0.1:8765
```

nginx-filen skrives fra `deploy/nginx-site-tls.conf` ved hver install. Den gamle gemmes som `fontlume.com.bak` og lægges tilbage, hvis den nye ikke består `nginx -t`.
