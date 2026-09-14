# Hosting: https://fontlume.com/ladeagent/mcp (én URL, ingen installation for den der tester)

Ét script gør alt på serveren (clone/pull, venv, tests, systemd-service, nginx-site, TLS via certbot når DNS er på plads). Idempotent.

```bash
ssh hetzner 'curl -sL https://raw.githubusercontent.com/TacoBadger/ladeagent/main/deploy/install.sh | bash'
```

Forudsætning: A-record for `fontlume.com` og `www.fontlume.com` peger på serverens IP (Hostinger DNS). Scriptet siger selv, hvis DNS ikke er på plads endnu, og kan bare køres igen bagefter.

Manuel test udefra:

```bash
curl -s -X POST https://fontlume.com/ladeagent/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Brug det:
- **claude.ai**: Indstillinger → Connectors → Tilføj custom connector → URL `https://fontlume.com/ladeagent/mcp`, ingen auth.
- **Claude Desktop**: samme connector-menu.
- **Claude Code**: `claude mcp add --transport http ladeagent https://fontlume.com/ladeagent/mcp`

Servicen kører med `--live`, så priserne er dagens rigtige day-ahead-priser (snapshottet bruges kun til evals). Logs: `journalctl -u ladeagent-mcp -f`.
