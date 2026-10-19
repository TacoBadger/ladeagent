#!/usr/bin/env bash
# Installerer/opdaterer ladeagenten på serveren. Idempotent: kan køres igen.
#   ssh hetzner 'curl -sL https://raw.githubusercontent.com/TacoBadger/ladeagent/main/deploy/install.sh | bash'
set -euo pipefail
BASE=${LADEAGENT_BASE:-https://in10x.com/ladeagent}
cd /root
if [ -d ladeagent/.git ]; then cd ladeagent && git pull -q; else git clone -q https://github.com/TacoBadger/ladeagent && cd ladeagent; fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt pytest-asyncio
.venv/bin/pip install -q -e .
echo "tests: $(.venv/bin/python -m pytest -q 2>&1 | tail -1)"

# 1) MCP-service (Streamable HTTP på 127.0.0.1:8765, live data)
cp deploy/ladeagent-mcp.service /etc/systemd/system/ladeagent-mcp.service
systemctl daemon-reload
systemctl enable ladeagent-mcp >/dev/null 2>&1 || true
systemctl restart ladeagent-mcp
for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8765/mcp && break; sleep 1; done
echo "service: $(systemctl is-active ladeagent-mcp)"
curl -s -o /dev/null -w "local mcp initialize: HTTP %{http_code}\n" -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'

# 2) Forsiden: statiske filer fra site/. nginx kan ikke læse /root, så de kopieres til /var/www.
rm -rf /var/www/ladeagent.new
cp -r site /var/www/ladeagent.new
# Runde 2-tallene (evals/reports/quality.json) læses af forsiden som /quality.json
[ -f evals/reports/quality.json ] && cp evals/reports/quality.json /var/www/ladeagent.new/quality.json
chmod -R a+rX /var/www/ladeagent.new
rm -rf /var/www/ladeagent.old
[ -d /var/www/ladeagent ] && mv /var/www/ladeagent /var/www/ladeagent.old
mv /var/www/ladeagent.new /var/www/ladeagent
rm -rf /var/www/ladeagent.old
echo "forside: $(find /var/www/ladeagent -type f | wc -l) filer i /var/www/ladeagent"

# 3) nginx-site for domænet. Skrives fra repoet hver gang. Den gamle fil gemmes,
#    og den kommer tilbage, hvis den nye ikke består nginx -t.
# nginx/TLS styres af in10x.com-sitet på serveren (location /ladeagent/ med stiomskrivning), ikke af dette script.
systemctl reload nginx
H='Content-Type: application/json'
curl -s -o /dev/null -w "public mcp initialize: HTTP %{http_code}\n" -X POST $BASE/mcp \
  -H "$H" -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
curl -s -o /dev/null -w "public forside: HTTP %{http_code}\n" $BASE/
curl -s -o /dev/null -w "public api status: HTTP %{http_code}\n" $BASE/api/status
echo "KLAR: $BASE/ og $BASE/mcp"
