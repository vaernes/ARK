#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK Local Laptop Dev Stack (GPU-less, no docker-compose required)
# Usage:
#   ./scripts/dev-up.sh         # Start lightweight dev stack (no Tika/OpenWebUI)
#   ./scripts/dev-up.sh --full  # Also start Apache Tika & Open WebUI (pulls images)
#   ./scripts/dev-up.sh --down  # Stop and remove dev containers & network
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

NET_NAME="ark-dev-net"
CONTROL_NET="ark-dev-control"
STATE_DIR="${REPO_ROOT}/.dev-state"
PG_PASS="ark-dev-pg-pass"
KC_PASS="ark-dev-kc-pass"
QDRANT_KEY="ark-dev-qdrant-key"
VLLM_KEY="ark-dev-vllm-key"
DEV_USER="arkadmin"
DEV_PASS="ark-dev-password"

log() { printf '[%s] [DEV] %s\n' "$(date -u +'%H:%M:%S')" "$*"; }

stop_stack() {
  log "Stopping ARK dev containers and network..."
  docker rm -f \
    ark-dev-caddy \
    ark-dev-ark-admin \
    ark-dev-oauth2-proxy \
    ark-dev-open-webui \
    ark-dev-tika \
    ark-dev-keycloak \
    ark-dev-vllm \
    ark-dev-tei-embed \
    ark-dev-tei-rerank \
    ark-dev-qdrant \
    ark-dev-postgres >/dev/null 2>&1 || true
  docker network rm "${NET_NAME}" "${CONTROL_NET}" >/dev/null 2>&1 || true
  rm -rf "${STATE_DIR}"
  log "ARK dev stack stopped."
}

FULL_STACK=false
if [[ "${1:-}" == "--down" ]]; then
  stop_stack
  exit 0
elif [[ "${1:-}" == "--full" ]]; then
  FULL_STACK=true
fi

# Clean any stale dev run first
stop_stack >/dev/null 2>&1 || true

COOKIE_SECRET="$(python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
PROXY_AUTH_SECRET="$(python3 -c 'import os,secrets; print(secrets.token_hex(32))')"
mkdir -p "${STATE_DIR}/backups" "${STATE_DIR}/audit" "${STATE_DIR}/keycloak"
cp "${REPO_ROOT}/config/keycloak/ark-realm.json" "${STATE_DIR}/keycloak/ark-realm.json"
sed -i "s|https://ark\.local|https://localhost:8443|g" "${STATE_DIR}/keycloak/ark-realm.json"

log "Building ark-admin:latest..."
docker build -q -t ark-admin:latest "${REPO_ROOT}/admin" >/dev/null

log "Creating networks '${NET_NAME}' and '${CONTROL_NET}'..."
docker network create "${NET_NAME}" >/dev/null
docker network create --internal "${CONTROL_NET}" >/dev/null

log "Starting PostgreSQL, Qdrant, and Mock Inference (vLLM + TEI Embed + TEI Rerank)..."
docker run -d --name ark-dev-postgres --network "${NET_NAME}" --network-alias postgres \
  -e POSTGRES_USER=ark_webui \
  -e POSTGRES_PASSWORD="${PG_PASS}" \
  -e POSTGRES_DB=openwebui \
  -v "${REPO_ROOT}/config/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro" \
  postgres:16-alpine >/dev/null

docker run -d --name ark-dev-qdrant --network "${NET_NAME}" --network-alias qdrant \
  -e QDRANT__TELEMETRY_DISABLED=true \
  -e QDRANT__SERVICE__API_KEY="${QDRANT_KEY}" \
  -v "${REPO_ROOT}/config/qdrant/config.yaml:/qdrant/config/production.yaml:ro" \
  qdrant/qdrant:v1.12.5 >/dev/null

docker run -d --name ark-dev-vllm --network "${NET_NAME}" --network-alias vllm \
  -v "${REPO_ROOT}/tests:/tests:ro" \
  python:3.12-slim-bookworm python3 /tests/mock_inference.py 8000 >/dev/null

docker run -d --name ark-dev-tei-embed --network "${NET_NAME}" --network-alias tei-embed \
  -v "${REPO_ROOT}/tests:/tests:ro" \
  python:3.12-slim-bookworm python3 /tests/mock_inference.py 80 >/dev/null

docker run -d --name ark-dev-tei-rerank --network "${NET_NAME}" --network-alias tei-rerank \
  -v "${REPO_ROOT}/tests:/tests:ro" \
  python:3.12-slim-bookworm python3 /tests/mock_inference.py 80 >/dev/null

for _ in $(seq 1 30); do
  if docker exec ark-dev-postgres pg_isready -U ark_webui -d ark_admin >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

log "Starting Keycloak..."
docker run -d --name ark-dev-keycloak --network "${NET_NAME}" --network-alias keycloak \
  -e KC_DB=postgres \
  -e KC_DB_URL="jdbc:postgresql://postgres:5432/keycloak" \
  -e KC_DB_USERNAME=ark_webui \
  -e KC_DB_PASSWORD="${PG_PASS}" \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
  -e KC_BOOTSTRAP_ADMIN_PASSWORD="${KC_PASS}" \
  -e KC_HEALTH_ENABLED=true \
  -v "${STATE_DIR}/keycloak:/opt/keycloak/data/import:ro" \
  quay.io/keycloak/keycloak:26.0 \
  start --import-realm --http-enabled=true --http-relative-path=/auth --hostname=https://localhost:8443/auth --hostname-strict=false --hostname-backchannel-dynamic=true --proxy-headers=xforwarded >/dev/null

log "Waiting for Keycloak readiness (up to 120s)..."
for i in $(seq 1 120); do
  if docker exec ark-dev-keycloak bash -c 'exec 3<>/dev/tcp/127.0.0.1/9000 && echo -e "GET /auth/health/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n" >&3 && grep -q "200 OK" <&3' >/dev/null 2>&1; then
    break
  fi
  if [[ "$i" -eq 120 ]]; then
    docker logs --tail 30 ark-dev-keycloak >&2 || true
    exit 1
  fi
  sleep 1
done

log "Seeding local dev admin user '${DEV_USER}' in Keycloak 'ark-admins' group..."
docker exec ark-dev-keycloak /opt/keycloak/bin/kcadm.sh config credentials \
  --server http://localhost:8080/auth --realm master --user admin --password "${KC_PASS}" >/dev/null
docker exec ark-dev-keycloak /opt/keycloak/bin/kcadm.sh create users -r ark \
  -s username="${DEV_USER}" -s email="arkadmin@ark.local" -s emailVerified=true -s firstName="ARK" -s lastName="Admin" -s enabled=true >/dev/null
docker exec ark-dev-keycloak /opt/keycloak/bin/kcadm.sh set-password -r ark \
  --username "${DEV_USER}" --new-password "${DEV_PASS}" >/dev/null
USER_ID="$(docker exec ark-dev-keycloak /opt/keycloak/bin/kcadm.sh get users -r ark -q username="${DEV_USER}" --fields id --format csv --noquotes | tr -d '\r')"
GROUP_ID="$(docker exec ark-dev-keycloak /opt/keycloak/bin/kcadm.sh get groups -r ark --fields id,name --format csv --noquotes | awk -F, '$2=="ark-admins"{print $1}' | tr -d '\r')"
docker exec ark-dev-keycloak /opt/keycloak/bin/kcadm.sh update "users/${USER_ID}/groups/${GROUP_ID}" -r ark -s realm=ark -s userId="${USER_ID}" -s groupId="${GROUP_ID}" -n >/dev/null

log "Starting oauth2-proxy, ark-admin, and Caddy (https://localhost:8443)..."
docker run -d --name ark-dev-oauth2-proxy --network "${NET_NAME}" --network-alias oauth2-proxy \
  quay.io/oauth2-proxy/oauth2-proxy:v7.7.1 \
  --provider=oidc \
  --skip-oidc-discovery=true \
  --oidc-issuer-url=https://localhost:8443/auth/realms/ark \
  --login-url=https://localhost:8443/auth/realms/ark/protocol/openid-connect/auth \
  --redeem-url=http://keycloak:8080/auth/realms/ark/protocol/openid-connect/token \
  --oidc-jwks-url=http://keycloak:8080/auth/realms/ark/protocol/openid-connect/certs \
  --profile-url=http://keycloak:8080/auth/realms/ark/protocol/openid-connect/userinfo \
  --client-id=oauth2-proxy \
  --client-secret=ark-oauth2-proxy-secret-change-me \
  --cookie-secret="${COOKIE_SECRET}" \
  --redirect-url=https://localhost:8443/oauth2/callback \
  --http-address=0.0.0.0:4180 \
  --email-domain=* \
  --insecure-oidc-allow-unverified-email=true \
  --oidc-groups-claim=groups \
  --set-xauthrequest=true \
  --pass-user-headers=true \
  --cookie-secure=true \
  --skip-provider-button=true >/dev/null

docker run -d --name ark-dev-ark-admin --network "${NET_NAME}" --network-alias ark-admin \
  -e ARK_ADMIN_DB_URL="postgresql://ark_webui:${PG_PASS}@postgres:5432/ark_admin" \
  -e POSTGRES_HOST=postgres \
  -e POSTGRES_PORT=5432 \
  -e POSTGRES_USER=ark_webui \
  -e POSTGRES_PASSWORD="${PG_PASS}" \
  -e QDRANT_URL="http://qdrant:6333" \
  -e QDRANT_API_KEY="${QDRANT_KEY}" \
  -e VLLM_URL="http://vllm:8000" \
  -e VLLM_API_KEY="${VLLM_KEY}" \
  -e LLM_SERVED_NAME="ark-llm" \
  -e TEI_EMBED_URL="http://tei-embed:80" \
  -e TEI_RERANK_URL="http://tei-rerank:80" \
  -e TIKA_URL="http://tika:9998" \
  -e OPENWEBUI_INTERNAL_URL="http://open-webui:8080" \
  -e KEYCLOAK_INTERNAL_URL="http://keycloak:8080/auth" \
  -e ARK_BACKUP_DIR="/backups" \
  -e ARK_AUDIT_LOG_FILE="/var/log/ark/audit.jsonl" \
  -e ARK_PROXY_AUTH_SECRET="${PROXY_AUTH_SECRET}" \
  -v "${STATE_DIR}/backups:/backups" \
  -v "${STATE_DIR}/audit:/var/log/ark" \
  ark-admin:latest >/dev/null
docker network connect --alias ark-admin-control "${CONTROL_NET}" ark-dev-ark-admin >/dev/null

if [[ "${FULL_STACK}" == true ]]; then
  log "Starting Apache Tika & Open WebUI (pulling images if not cached)..."
  mkdir -p "${STATE_DIR}/open-webui"
  docker run -d --name ark-dev-tika --network "${NET_NAME}" --network-alias tika \
    -e DO_NOT_TRACK=1 \
    apache/tika:3.1.0.0-full >/dev/null

  docker run -d --name ark-dev-open-webui --network "${NET_NAME}" --network-alias open-webui \
    -e OFFLINE_MODE=true \
    -e HF_HUB_OFFLINE=1 \
    -e TRANSFORMERS_OFFLINE=1 \
    -e DO_NOT_TRACK=1 \
    -e ANONYMIZED_TELEMETRY=false \
    -e SCARF_NO_ANALYTICS=true \
    -e ENABLE_VERSION_UPDATE_CHECK=false \
    -e ENABLE_COMMUNITY_SHARING=false \
    -e ENABLE_OLLAMA_API=false \
    -e DATABASE_URL="postgresql://ark_webui:${PG_PASS}@postgres:5432/openwebui" \
    -e WEBUI_NAME="ARK — Air-Gapped Retrieval & Knowledge" \
    -e WEBUI_SECRET_KEY="ark-dev-webui-secret-key" \
    -e WEBUI_URL="https://localhost:8443" \
    -e ENABLE_SIGNUP=false \
    -e ENABLE_LOGIN_FORM=false \
    -e DEFAULT_USER_ROLE=user \
    -e ENABLE_OAUTH_SIGNUP=true \
    -e OAUTH_PROVIDER_NAME="Corporate SSO" \
    -e OPENID_PROVIDER_URL="http://keycloak:8080/auth/realms/ark/.well-known/openid-configuration" \
    -e OAUTH_CLIENT_ID="open-webui" \
    -e OAUTH_CLIENT_SECRET="ark-openwebui-oidc-secret-change-me" \
    -e OAUTH_SCOPES="openid email profile groups" \
    -e ENABLE_OAUTH_GROUP_MANAGEMENT=true \
    -e ENABLE_OAUTH_GROUP_CREATION=true \
    -e OAUTH_GROUPS_CLAIM="groups" \
    -e OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true \
    -e OPENAI_API_BASE_URL="http://vllm:8000/v1" \
    -e OPENAI_API_KEY="${VLLM_KEY}" \
    -e VECTOR_DB=qdrant \
    -e QDRANT_URI="http://qdrant:6333" \
    -e QDRANT_API_KEY="${QDRANT_KEY}" \
    -e RAG_EMBEDDING_ENGINE=openai \
    -e RAG_EMBEDDING_OPENAI_API_BASE_URL="http://tei-embed:80/v1" \
    -e RAG_EMBEDDING_OPENAI_API_KEY="local-tei" \
    -e RAG_EMBEDDING_MODEL="BAAI/bge-m3" \
    -e CONTENT_EXTRACTION_ENGINE=tika \
    -e TIKA_SERVER_URL="http://tika:9998" \
    -v "${STATE_DIR}/open-webui:/app/backend/data" \
    -v "${REPO_ROOT}/admin/static/ark-logo.svg:/app/backend/open_webui/static/favicon.svg:ro" \
    -v "${REPO_ROOT}/config/open-webui/custom.css:/app/backend/open_webui/static/custom.css:ro" \
    ghcr.io/open-webui/open-webui:v0.5.10 \
    bash -c 'grep -q "/static/custom.css" /app/build/index.html || sed -i "s|</head>|<link rel=\"stylesheet\" href=\"/static/custom.css\" /></head>|" /app/build/index.html; exec bash start.sh' >/dev/null
fi

docker run -d --name ark-dev-caddy --network "${NET_NAME}" --network-alias caddy \
  -p 127.0.0.1:8443:443 \
  -e ARK_DOMAIN="localhost" \
  -e ARK_PROXY_AUTH_SECRET="${PROXY_AUTH_SECRET}" \
  -v "${REPO_ROOT}/config/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2-alpine >/dev/null
docker network connect "${CONTROL_NET}" ark-dev-caddy >/dev/null

cat <<EOF

====================================================================
ARK Dev Stack is RUNNING!
====================================================================
  Admin Console:    https://localhost:8443/admin-console/
  SSO Login:        username: ${DEV_USER}   password: ${DEV_PASS}
  Keycloak Admin:   https://localhost:8443/auth/admin/
  KC Master Login:  username: admin         password: ${KC_PASS}

  Stop & clean up:  ./scripts/dev-up.sh --down
====================================================================
EOF
