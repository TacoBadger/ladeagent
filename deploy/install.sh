#!/usr/bin/env bash
# Installerer/opdaterer ladeagenten på serveren. Idempotent: kan køres igen.
#   ssh hetzner 'curl -sL https://raw.githubusercontent.com/TacoBadger/ladeagent/main/deploy/install.sh | bash'
set -euo pipefail
DOMAIN=${LADEAGENT_DOMAIN:-fontlume.com}
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
sleep 4
echo "service: $(systemctl is-active ladeagent-mcp)"
curl -s -o /dev/null -w "local mcp initialize: HTTP %{http_code}\n" -X POST http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'

# 2) Forsiden: statiske filer fra site/. nginx kan ikke læse /root, så de kopieres til /var/www.
rm -rf /var/www/ladeagent.new
cp -r site /var/www/ladeagent.new
chmod -R a+rX /var/www/ladeagent.new
rm -rf /var/www/ladeagent.old
[ -d /var/www/ladeagent ] && mv /var/www/ladeagent /var/www/ladeagent.old
mv /var/www/ladeagent.new /var/www/ladeagent
rm -rf /var/www/ladeagent.old
echo "forside: $(find /var/www/ladeagent -type f | wc -l) filer i /var/www/ladeagent"

# 3) nginx-site for domænet. Skrives fra repoet hver gang. Den gamle fil gemmes,
#    og den kommer tilbage, hvis den nye ikke består nginx -t.
SITE=/etc/nginx/sites-available/$DOMAIN
if [ -d /etc/letsencrypt/live/$DOMAIN ]; then TEMPLATE=deploy/nginx-site-tls.conf; else TEMPLATE=deploy/nginx-site.conf; fi
[ -f $SITE ] && cp $SITE $SITE.bak
sed "s/__DOMAIN__/$DOMAIN/g" $TEMPLATE > $SITE
ln -sf $SITE /etc/nginx/sites-enabled/$DOMAIN
if nginx -t 2>/tmp/ladeagent-nginx-test.log; then
  systemctl reload nginx
  echo "nginx: ok ($TEMPLATE)"
else
  cat /tmp/ladeagent-nginx-test.log
  if [ -f $SITE.bak ]; then cp $SITE.bak $SITE; else rm -f $SITE /etc/nginx/sites-enabled/$DOMAIN; fi
  nginx -t && systemctl reload nginx
  echo "nginx: den nye konfiguration fejlede, den gamle er lagt tilbage. Intet er ændret udadtil."
  exit 1
fi

# 4) TLS, når DNS peger på denne server
MYIP=$(curl -s -4 ifconfig.me)
DNSIP=$(dig +short A $DOMAIN | tail -1)
if [ "$DNSIP" = "$MYIP" ]; then
  if [ ! -d /etc/letsencrypt/live/$DOMAIN ]; then
    certbot --nginx -d $DOMAIN --non-interactive --agree-tos --redirect -m theispfrost@gmail.com
  fi
  echo "TLS: ok"
  curl -s -o /dev/null -w "public mcp initialize: HTTP %{http_code}\n" -X POST https://$DOMAIN/mcp \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
  curl -s -o /dev/null -w "public forside: HTTP %{http_code}\n" https://$DOMAIN/
  curl -s -o /dev/null -w "public api status: HTTP %{http_code}\n" https://$DOMAIN/api/status
  echo "KLAR: https://$DOMAIN/ og https://$DOMAIN/mcp"
else
  echo "DNS for $DOMAIN peger på '$DNSIP', serveren er $MYIP. Tilføj A-record for '$DOMAIN' hos Hostinger der peger på $MYIP og kør scriptet igen for TLS."
fi
