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
SITE=/etc/nginx/sites-available/$DOMAIN
CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
write_nginx() {
  local template=deploy/nginx-site.conf www=""
  if [ -f $CERT ]; then
    template=deploy/nginx-site-tls.conf
    if openssl x509 -in $CERT -noout -text | grep -q "DNS:www.$DOMAIN"; then www=" www.$DOMAIN"; fi
  fi
  [ -f $SITE ] && cp $SITE $SITE.bak
  sed -e "s/__DOMAIN____WWW__/$DOMAIN$www/g" -e "s/__DOMAIN__/$DOMAIN/g" $template > $SITE
  ln -sf $SITE /etc/nginx/sites-enabled/$DOMAIN
  if nginx -t 2>/tmp/ladeagent-nginx-test.log; then
    systemctl reload nginx
    echo "nginx: ok ($template,$( [ -n "$www" ] && echo " med www" || echo " uden www"))"
  else
    cat /tmp/ladeagent-nginx-test.log
    if [ -f $SITE.bak ]; then cp $SITE.bak $SITE; else rm -f $SITE /etc/nginx/sites-enabled/$DOMAIN; fi
    nginx -t && systemctl reload nginx
    echo "nginx: den nye konfiguration fejlede, den gamle er lagt tilbage. Intet er ændret udadtil."
    exit 1
  fi
}
write_nginx

# 4) TLS, når DNS peger på denne server
MYIP=$(curl -s -4 ifconfig.me)
DNSIP=$(dig +short A $DOMAIN | tail -1)
if [ "$DNSIP" = "$MYIP" ]; then
  if [ ! -d /etc/letsencrypt/live/$DOMAIN ]; then
    certbot --nginx -d $DOMAIN --non-interactive --agree-tos --redirect -m theispfrost@gmail.com
  fi
  # www: når www peger på serveren, og certifikatet ikke dækker det endnu, udvides certifikatet
  WWWIP=$(dig +short A www.$DOMAIN | tail -1)
  if [ "$WWWIP" = "$MYIP" ] && ! openssl x509 -in $CERT -noout -text | grep -q "DNS:www.$DOMAIN"; then
    if certbot certonly --nginx --cert-name $DOMAIN -d $DOMAIN -d www.$DOMAIN --expand --non-interactive --agree-tos -m theispfrost@gmail.com; then
      write_nginx
    else
      echo "www: certifikatet kunne ikke udvides. $DOMAIN virker som før. Kør scriptet igen senere."
    fi
  elif [ "$WWWIP" != "$MYIP" ]; then
    echo "www: www.$DOMAIN peger på '$WWWIP', ikke på serveren. Springes over."
  fi
  echo "TLS: ok"
  curl -s -o /dev/null -w "public mcp initialize: HTTP %{http_code}\n" -X POST https://$DOMAIN/mcp \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
  curl -s -o /dev/null -w "public forside: HTTP %{http_code}\n" https://$DOMAIN/
  curl -s -o /dev/null -w "public api status: HTTP %{http_code}\n" https://$DOMAIN/api/status
  if openssl x509 -in $CERT -noout -text | grep -q "DNS:www.$DOMAIN"; then
    curl -s -o /dev/null -w "public www: HTTP %{http_code} -> %{redirect_url}\n" https://www.$DOMAIN/
  fi
  echo "KLAR: https://$DOMAIN/ og https://$DOMAIN/mcp"
else
  echo "DNS for $DOMAIN peger på '$DNSIP', serveren er $MYIP. Tilføj A-record for '$DOMAIN' hos Hostinger der peger på $MYIP og kør scriptet igen for TLS."
fi
