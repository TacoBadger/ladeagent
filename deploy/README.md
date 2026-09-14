# Hosting (én URL, ingen installation for den der tester)

1. `git clone https://github.com/TacoBadger/ladeagent /root/ladeagent && cd /root/ladeagent`
2. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e .`
3. `cp deploy/ladeagent-mcp.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now ladeagent-mcp`
4. Indsæt `deploy/nginx-location.conf` i det ønskede HTTPS-server-block, `nginx -t && systemctl reload nginx`
5. Test: `curl -s -X POST https://<domæne>/ladeagent/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'`

I claude.ai: Indstillinger → Connectors → Tilføj custom connector → URL `https://<domæne>/ladeagent/mcp`, ingen auth.
I Claude Code: `claude mcp add --transport http ladeagent https://<domæne>/ladeagent/mcp`.

Servicen kører med `--live`, så priserne er dagens rigtige day-ahead-priser (snapshottet bruges kun til evals).
