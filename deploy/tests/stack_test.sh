#!/bin/bash
# End-to-end test of the deployment configuration (Milestone 8.4e): the REAL nginx site and
# snippets from deploy/nginx in front of REAL Gunicorn + Django in production mode, using
# deploy/gunicorn.conf.py. Everything runs as a normal user on loopback high ports in a
# temporary directory; nothing is installed, no service is started, no secrets are used
# (fictional SECRET_KEY/API token, throwaway SQLite, self-signed one-day certificate).
#
#   deploy/tests/stack_test.sh --venv /path/to/venv        # venv with backend/requirements.txt
#   NGINX=/usr/sbin/nginx deploy/tests/stack_test.sh ...   # use an installed nginx binary
#
# Without NGINX, the Ubuntu nginx packages are fetched with `apt-get download` and only
# extracted into the temporary directory. frontend/dist must be built (npm run build).
# Placeholders mapped in a TEST COPY only: VPN address 10.99.0.1 -> 127.0.0.2, VPN subnet
# 10.99.0.0/24 -> 127.0.0.0/29, 443 -> 18443, 80 -> 18080, Gunicorn 8000 -> 18000.
# Exit status 0 when every check passes. Validated on Ubuntu 26.04 / nginx 1.28.3: 61 checks.
set -uo pipefail
VENV=""
while [ $# -gt 0 ]; do
  case "$1" in
    --venv) VENV=${2:?}; shift 2 ;;
    *) echo "usage: $0 --venv DIR" >&2; exit 2 ;;
  esac
done
[ -x "$VENV/bin/gunicorn" ] || { echo "--venv must point at a virtualenv with gunicorn installed" >&2; exit 2; }
REPO=$(cd "$(dirname "$0")/../.." && pwd)
[ -f "$REPO/frontend/dist/index.html" ] || { echo "build the frontend first: frontend/dist/index.html is missing" >&2; exit 2; }
for port in 18000 18080 18443; do
  ss -ltnH | awk '{print $4}' | grep -q ":$port\$" && { echo "port $port is in use" >&2; exit 2; }
done
SFX=$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
S=$(mktemp -d "${TMPDIR:-/tmp}/applyforge-stack-XXXXXXXX")
mkdir -p "$S"/{debs,root,snippets,ssl,logs,tmp,static,media}
PASS=0; FAILS=0
t() { if [ "$2" = "$3" ]; then PASS=$((PASS+1)); echo "PASS  $1"; else FAILS=$((FAILS+1)); echo "FAIL  $1  (expected [$2], got [$3])"; fi; }
has() { if grep -qiE "$2" <<<"$3"; then PASS=$((PASS+1)); echo "PASS  $1"; else FAILS=$((FAILS+1)); echo "FAIL  $1  (missing /$2/)"; fi; }
hasnt() { if grep -qiE "$2" <<<"$3"; then FAILS=$((FAILS+1)); echo "FAIL  $1  (unexpected /$2/)"; else PASS=$((PASS+1)); echo "PASS  $1"; fi; }

if [ -n "${NGINX:-}" ]; then
  echo "== nginx: using $NGINX"
  MIME=/etc/nginx/mime.types
else
  echo "== nginx: download and extract the distribution packages (no installation, no service)"
  (cd "$S/debs" && apt-get download nginx nginx-common >/dev/null 2>&1) || { echo "apt-get download failed"; exit 2; }
  for d in "$S"/debs/*.deb; do dpkg-deb -x "$d" "$S/root"; done
  NGINX=$(find "$S/root" -type f -name nginx -path '*sbin*' | head -1)
  MIME=$(find "$S/root" -name mime.types | head -1)
fi
echo "nginx binary: $NGINX ($("$NGINX" -v 2>&1))"

echo "== test certificate (self-signed, test only) and site config with placeholders mapped"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=applyforge.vpn.example" \
  -addext "subjectAltName=DNS:applyforge.vpn.example" -keyout "$S/ssl/applyforge.key" -out "$S/ssl/applyforge.crt" >/dev/null 2>&1
cp "$REPO"/deploy/nginx/snippets/*.conf "$S/snippets/"
sed -i 's#proxy_pass http://127.0.0.1:8000;#proxy_pass http://127.0.0.1:18000;#' "$S/snippets/applyforge-proxy.conf"
sed -e "s#10.99.0.1:443#127.0.0.2:18443#g; s#10.99.0.1:80#127.0.0.2:18080#g" \
    -e "s#allow 10.99.0.1;#allow 127.0.0.2;#; s#allow 10.99.0.0/24;#allow 127.0.0.0/29;#g" \
    -e "s#return 301 https://applyforge.vpn.example\$request_uri;#return 301 https://applyforge.vpn.example:18443\$request_uri;#" \
    -e "s#/etc/nginx/snippets/#$S/snippets/#g; s#/etc/ssl/applyforge/#$S/ssl/#g; s#/var/log/nginx/#$S/logs/#g" \
    -e "s#/srv/applyforge/current/frontend/dist#$REPO/frontend/dist#; s#/var/lib/applyforge/static/#$S/static/#" \
    "$REPO/deploy/nginx/applyforge.conf" > "$S/applyforge.conf"
echo "-- substitutions (test copy only):"; diff "$REPO/deploy/nginx/applyforge.conf" "$S/applyforge.conf" | grep '^>' | sed 's/^> */   /'
cat > "$S/nginx.conf" <<EOF
pid $S/nginx.pid;
error_log $S/logs/error.log warn;
events { worker_connections 256; }
http {
    include $MIME;
    default_type application/octet-stream;
    access_log $S/logs/access.log;      # as the server's /etc/nginx/nginx.conf provides
    client_body_temp_path $S/tmp/body; proxy_temp_path $S/tmp/proxy;
    fastcgi_temp_path $S/tmp/fastcgi; uwsgi_temp_path $S/tmp/uwsgi; scgi_temp_path $S/tmp/scgi;
    include $S/applyforge.conf;
}
EOF
echo "== nginx -t (the real deployment config)"
"$NGINX" -t -p "$S/" -c "$S/nginx.conf" 2>&1 | sed 's/^/   /'
t "nginx -t accepts the deployment configuration" 0 "${PIPESTATUS[0]}"

echo "== Django in production mode (fictional secrets, throwaway SQLite), migrate + collectstatic"
TOKEN="fictional-stack-token-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
export DEBUG=False APPLYFORGE_DOTENV=/nonexistent/applyforge-no-dotenv
export SECRET_KEY="fictional-$(od -An -N40 -tx1 /dev/urandom | tr -d ' \n')" API_TOKEN="$TOKEN"
export DATABASE_URL="sqlite:///$S/stack.sqlite3" ALLOWED_HOSTS=applyforge.vpn.example
export CSRF_TRUSTED_ORIGINS=https://applyforge.vpn.example CORS_ALLOWED_ORIGINS=
export SECURE_PROXY_SSL_HEADER=HTTP_X_FORWARDED_PROTO,https SECURE_HSTS_INCLUDE_SUBDOMAINS=False SECURE_HSTS_PRELOAD=False
export STATIC_ROOT="$S/static" MEDIA_ROOT="$S/media" PYTHONDONTWRITEBYTECODE=1
cd "$REPO/backend"
"$VENV/bin/python" manage.py migrate --noinput >"$S/logs/migrate.log" 2>&1; t "migrate (throwaway SQLite)" 0 $?
"$VENV/bin/python" manage.py collectstatic --noinput >"$S/logs/collectstatic.log" 2>&1; t "collectstatic into STATIC_ROOT" 0 $?
"$VENV/bin/python" manage.py check --deploy >"$S/logs/check.log" 2>&1
has "check --deploy: only the two intentional HSTS warnings" "W005" "$(cat "$S/logs/check.log")"
hasnt "check --deploy: no other warnings/errors" "W0(0[0-46-9]|1|2[02-9])|E[0-9]{3}" "$(grep -oE '\((security|[a-z_]+)\.[WE][0-9]+\)' "$S/logs/check.log" | grep -v -e W005 -e W021)"

echo "== start Gunicorn with deploy/gunicorn.conf.py (bind overridden to 127.0.0.1:18000) and nginx"
"$VENV/bin/gunicorn" --config "$REPO/deploy/gunicorn.conf.py" --bind 127.0.0.1:18000 >"$S/logs/gunicorn.log" 2>&1 &
GPID=$!
"$NGINX" -p "$S/" -c "$S/nginx.conf"
for i in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:18000/ && break; sleep 0.25; done
trap 'kill $GPID 2>/dev/null; "$NGINX" -p "$S/" -c "$S/nginx.conf" -s quit 2>/dev/null' EXIT
echo "   listening: $(ss -ltnH | awk '{print $4}' | grep -E ':(18000|18443|18080)$' | sort | paste -sd' ')"
t "Gunicorn listens on 127.0.0.1 only" "127.0.0.1:18000" "$(ss -ltnH | awk '{print $4}' | grep ':18000$')"

C="curl -s --cacert $S/ssl/applyforge.crt --resolve applyforge.vpn.example:18443:127.0.0.2"
U=https://applyforge.vpn.example:18443
code() { $C -o /dev/null -w '%{http_code}' "$@"; }
hdrs() { $C -s -D - -o /dev/null "$@"; }

echo "== HTTPS, SPA and static files"
H=$(hdrs "$U/"); t "GET / -> 200" 200 "$(code "$U/")"
has "index.html served (SPA root)" '<div id="root">' "$($C "$U/")"
has "HSTS one year" '^strict-transport-security: max-age=31536000' "$H"
hasnt "HSTS without includeSubDomains/preload" 'includeSubDomains|preload' "$(grep -i '^strict-transport' <<<"$H")"
has "Content-Security-Policy present" "^content-security-policy: default-src 'self'" "$H"
has "X-Frame-Options DENY" '^x-frame-options: DENY' "$H"
has "index.html not cached" '^cache-control: no-store' "$H"
has "server version hidden" '^server: nginx\s*$' "$(tr -d '\r' <<<"$H")"
t "SPA fallback /applications/12 -> 200" 200 "$(code "$U/applications/12")"
has "SPA fallback serves index.html" '<div id="root">' "$($C "$U/jobs/7")"
ASSET=$(ls "$REPO/frontend/dist/assets" | grep '\.js$' | head -1)
t "hashed asset -> 200" 200 "$(code "$U/assets/$ASSET")"
has "asset cached immutable" 'immutable' "$(hdrs "$U/assets/$ASSET")"
t "missing asset -> 404 (not the SPA)" 404 "$(code "$U/assets/missing-$SFX.js")"
t "collected static file -> 200" 200 "$(code "$U/static/admin/css/base.css")"
t "dotfile -> 404" 404 "$(code "$U/.env")"

echo "== blocked paths"
for p in /admin/ /admin /admin/login/ /%61dmin/ //admin/ /media/x /media/resumes/fictional.pdf; do t "GET $p -> 404" 404 "$(code "$U$p")"; done

echo "== API, health and forwarded headers"
t "health -> 200" 200 "$(code "$U/api/v1/health/")"
t "health body" '{"status": "ok", "database": "ok"}' "$($C "$U/api/v1/health/")"
HH=$(hdrs "$U/api/v1/health/")
t "exactly one HSTS header on API responses" 1 "$(grep -ci '^strict-transport-security' <<<"$HH")"
has "health never cached" '^cache-control: .*no-store' "$HH"
t "spoofed X-Forwarded-Proto: http is overwritten (no redirect)" 200 "$(code -H 'X-Forwarded-Proto: http' "$U/api/v1/health/")"
t "spoofed X-Forwarded-Ssl/Protocol cleared (no 400)" 200 "$(code -H 'X-Forwarded-Ssl: off' -H 'X-Forwarded-Protocol: http' "$U/api/v1/health/")"
t "API without token -> 401" 401 "$(code "$U/api/v1/profiles/")"
t "API with token -> 200" 200 "$(code -H "Authorization: Bearer $TOKEN" "$U/api/v1/profiles/")"
t "collector run history readable (GET) -> 200" 200 "$(code -H "Authorization: Bearer $TOKEN" "$U/api/v1/collector/runs/")"
has "Gunicorn access log shows the real client (X-Forwarded-For)" '^127\.0\.0\.1 "GET /api/v1/health/' "$(cat "$S/logs/gunicorn.log")"

echo "== collector and generation disabled at nginx (incl. bypass attempts)"
for p in /api/v1/collector/runs/ /api/v1/collector/runs /api/v1/collector/%72uns/ //api/v1/collector/runs/ /api/v1/collector/./runs/ /api/v1/collector/runs.json; do
  t "POST $p -> 403" 403 "$(code -X POST -H "Authorization: Bearer $TOKEN" "$U$p")"
done
for m in PUT PATCH DELETE OPTIONS; do t "$m collector -> 403" 403 "$(code -X $m "$U/api/v1/collector/runs/")"; done
t "collector 403 body is JSON detail" '{"detail":"Collection is disabled on this server."}' "$($C -X POST "$U/api/v1/collector/runs/")"
for p in /api/v1/applications/1/generate/ /api/v1/applications/1/generate /api/v1/applications/%201/generate/ /api/v1/applications/1/generate.json //api/v1/applications/1/generate/; do
  t "POST $p -> 403" 403 "$(code -X POST -H "Authorization: Bearer $TOKEN" "$U$p")"
done

echo "== request limits"
head -c $((13*1024*1024)) /dev/zero > "$S/tmp/big.bin"
t "13 MiB upload -> 413 (client_max_body_size 12m)" 413 "$(code -X POST -H "Authorization: Bearer $TOKEN" -F "file=@$S/tmp/big.bin" "$U/api/v1/resumes/")"

echo "== HTTP redirect, unknown hosts, VPN allowlist"
t "HTTP -> 301" 301 "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: applyforge.vpn.example' http://127.0.0.2:18080/jobs)"
t "HTTP redirect target is the fixed HTTPS host" "https://applyforge.vpn.example:18443/jobs" "$(curl -s -o /dev/null -w '%{redirect_url}' -H 'Host: applyforge.vpn.example' http://127.0.0.2:18080/jobs)"
curl -s -o /dev/null -H 'Host: evil.example' http://127.0.0.2:18080/; t "HTTP unknown Host -> connection closed (444)" 52 $?
curl -s -o /dev/null --cacert "$S/ssl/applyforge.crt" --resolve evil.example:18443:127.0.0.2 https://evil.example:18443/; rc=$?
t "HTTPS unknown SNI -> TLS handshake rejected" yes "$([ $rc -eq 35 ] || [ $rc -eq 56 ] && echo yes || echo "no(rc=$rc)")"
t "client outside the VPN subnet -> 403" 403 "$($C --interface 127.0.0.9 -o /dev/null -w '%{http_code}' "$U/api/v1/health/")"
t "client outside the VPN subnet cannot reach the SPA" 403 "$($C --interface 127.0.0.9 -o /dev/null -w '%{http_code}' "$U/")"

echo "== nginx error log (warnings and above)"; sed 's/^/   /' "$S/logs/error.log" | tail -n 5
echo
echo "STACK TEST: $PASS passed, $FAILS failed"
kill $GPID 2>/dev/null; wait $GPID 2>/dev/null; "$NGINX" -p "$S/" -c "$S/nginx.conf" -s quit 2>/dev/null; sleep 0.5
trap - EXIT
rm -rf "$S"; echo "removed $S"
[ $FAILS -eq 0 ]
