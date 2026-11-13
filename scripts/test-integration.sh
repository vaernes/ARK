#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# ARK GPU-less End-to-End Integration Test
# Runs Postgres, Qdrant, Keycloak, OAuth2-Proxy, Mock Inference, ark-admin,
# and Caddy on an isolated Docker network to verify all integrations.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

KEEP_RUNNING=false
if [[ "${1:-}" == "--keep" ]]; then
  KEEP_RUNNING=true
fi

NET_NAME="ark-test-net"
CONTROL_NET="ark-test-control"
TMP_ROOT="$(mktemp -d /tmp/ark-test-XXXXXX)"
PG_PASS="ark-test-pg-pass"
KC_PASS="ark-test-kc-pass"
QDRANT_KEY="ark-test-qdrant-key"
VLLM_KEY="ark-test-vllm-key"
COOKIE_SECRET="$(python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
PROXY_AUTH_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

log()  { printf '[%s] [TEST] %s\n' "$(date -u +'%H:%M:%S')" "$*"; }
pass() { printf '[%s] [PASS] %s\n' "$(date -u +'%H:%M:%S')" "$*"; }
fail() { printf '[%s] [FAIL] %s\n' "$(date -u +'%H:%M:%S')" "$*" >&2; exit 1; }

cleanup() {
  if [[ "${KEEP_RUNNING}" == true ]]; then
    log "Containers left running (--keep). Clean up manually with: docker rm -f \$(docker ps -aq --filter name=ark-test-)"
    return
  fi
  log "Cleaning up test containers and temporary files..."
  docker exec ark-test-ark-admin rm -rf /backups/* /var/log/ark/* >/dev/null 2>&1 || true
  docker rm -f \
    ark-test-caddy \
    ark-test-ark-admin \
    ark-test-oauth2-proxy \
    ark-test-keycloak \
    ark-test-vllm \
    ark-test-tei-embed \
    ark-test-tei-rerank \
    ark-test-qdrant \
    ark-test-postgres >/dev/null 2>&1 || true
  docker network rm "${NET_NAME}" "${CONTROL_NET}" >/dev/null 2>&1 || true
  rm -rf "${TMP_ROOT}" 2>/dev/null || true
}
trap cleanup EXIT

main() {
  mkdir -p "${TMP_ROOT}/backups" "${TMP_ROOT}/audit" "${TMP_ROOT}/keycloak" "${TMP_ROOT}/plugins" "${TMP_ROOT}/ingest-scripts"
  cp "${REPO_ROOT}/config/keycloak/ark-realm.json" "${TMP_ROOT}/keycloak/ark-realm.json"
  sed -i "s|https://ark\.local|https://localhost:8443|g" "${TMP_ROOT}/keycloak/ark-realm.json"

  ARK_VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION")"
  GIT_COMMIT="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
  BUILD_DATE="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  log "Building ark-admin image (ARK v${ARK_VERSION})..."
  docker build -q \
    --build-arg "ARK_VERSION=${ARK_VERSION}" \
    --build-arg "ARK_GIT_COMMIT=${GIT_COMMIT}" \
    --build-arg "ARK_BUILD_DATE=${BUILD_DATE}" \
    -t ark-admin:latest "${REPO_ROOT}/admin" >/dev/null
  img_ver=$(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' ark-admin:latest)
  [[ "${img_ver}" == "${ARK_VERSION}" ]] || fail "Expected OCI image version label '${ARK_VERSION}', got '${img_ver}'"

  log "Creating isolated Docker networks '${NET_NAME}' and '${CONTROL_NET}'..."
  docker network create "${NET_NAME}" >/dev/null
  docker network create --internal "${CONTROL_NET}" >/dev/null

  log "Starting PostgreSQL 16..."
  docker run -d --name ark-test-postgres --network "${NET_NAME}" --network-alias postgres \
    -e POSTGRES_USER=ark_webui \
    -e POSTGRES_PASSWORD="${PG_PASS}" \
    -e POSTGRES_DB=openwebui \
    -v "${REPO_ROOT}/config/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro" \
    postgres:16-alpine >/dev/null

  log "Starting Qdrant..."
  docker run -d --name ark-test-qdrant --network "${NET_NAME}" --network-alias qdrant \
    -e QDRANT__TELEMETRY_DISABLED=true \
    -e QDRANT__SERVICE__API_KEY="${QDRANT_KEY}" \
    -v "${REPO_ROOT}/config/qdrant/config.yaml:/qdrant/config/production.yaml:ro" \
    qdrant/qdrant:v1.12.5 >/dev/null

  log "Starting Mock vLLM & Mock TEI..."
  docker run -d --name ark-test-vllm --network "${NET_NAME}" --network-alias vllm \
    -v "${REPO_ROOT}/tests:/tests:ro" \
    python:3.12-slim-bookworm python3 /tests/mock_inference.py 8000 >/dev/null

  docker run -d --name ark-test-tei-embed --network "${NET_NAME}" --network-alias tei-embed \
    -v "${REPO_ROOT}/tests:/tests:ro" \
    python:3.12-slim-bookworm python3 /tests/mock_inference.py 80 >/dev/null

  docker run -d --name ark-test-tei-rerank --network "${NET_NAME}" --network-alias tei-rerank \
    -v "${REPO_ROOT}/tests:/tests:ro" \
    python:3.12-slim-bookworm python3 /tests/mock_inference.py 80 >/dev/null

  log "Waiting for PostgreSQL readiness..."
  for _ in $(seq 1 30); do
    if docker exec ark-test-postgres pg_isready -U ark_webui -d ark_admin >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  log "Starting Keycloak (importing ark-realm.json)..."
  docker run -d --name ark-test-keycloak --network "${NET_NAME}" --network-alias keycloak \
    -e KC_DB=postgres \
    -e KC_DB_URL="jdbc:postgresql://postgres:5432/keycloak" \
    -e KC_DB_USERNAME=ark_webui \
    -e KC_DB_PASSWORD="${PG_PASS}" \
    -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
    -e KC_BOOTSTRAP_ADMIN_PASSWORD="${KC_PASS}" \
    -e KC_HEALTH_ENABLED=true \
    -v "${TMP_ROOT}/keycloak:/opt/keycloak/data/import:ro" \
    quay.io/keycloak/keycloak:26.0 \
    start --import-realm --http-enabled=true --http-relative-path=/auth --hostname=https://localhost:8443/auth --hostname-strict=false --hostname-backchannel-dynamic=true --proxy-headers=xforwarded >/dev/null

  log "Waiting for Keycloak readiness (up to 120s)..."
  for i in $(seq 1 120); do
    if docker exec ark-test-keycloak bash -c 'exec 3<>/dev/tcp/127.0.0.1/9000 && echo -e "GET /auth/health/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n" >&3 && grep -q "200 OK" <&3' >/dev/null 2>&1; then
      break
    fi
    if [[ "$i" -eq 120 ]]; then
      docker logs --tail 30 ark-test-keycloak >&2 || true
      fail "Keycloak did not become ready in time."
    fi
    sleep 1
  done
  pass "Keycloak is ready."

  log "Starting oauth2-proxy..."
  docker run -d --name ark-test-oauth2-proxy --network "${NET_NAME}" --network-alias oauth2-proxy \
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

  mkdir -p "${TMP_ROOT}/models/Qwen--Qwen2.5-14B-Instruct" "${TMP_ROOT}/models/Qwen--Qwen2.5-32B-Instruct"
  echo '{"model_type":"qwen2","architectures":["Qwen2ForCausalLM"]}' > "${TMP_ROOT}/models/Qwen--Qwen2.5-14B-Instruct/config.json"
  echo '{"model_type":"qwen2","architectures":["Qwen2ForCausalLM"]}' > "${TMP_ROOT}/models/Qwen--Qwen2.5-32B-Instruct/config.json"
  touch "${TMP_ROOT}/models/Qwen--Qwen2.5-14B-Instruct/model.safetensors" "${TMP_ROOT}/models/Qwen--Qwen2.5-32B-Instruct/model.safetensors"

  log "Starting ark-admin..."
  docker run -d --name ark-test-ark-admin --network "${NET_NAME}" --network-alias ark-admin \
    -e ARK_ADMIN_DB_URL="postgresql://ark_webui:${PG_PASS}@postgres:5432/ark_admin" \
    -e POSTGRES_HOST=postgres \
    -e POSTGRES_PORT=5432 \
    -e POSTGRES_USER=ark_webui \
    -e POSTGRES_PASSWORD="${PG_PASS}" \
    -e QDRANT_URL="http://qdrant:6333" \
    -e QDRANT_API_KEY="${QDRANT_KEY}" \
    -e LLM_BACKEND_TYPE="ark_llm" \
    -e LLM_BACKEND_ADMIN_URL="http://localhost:8095" \
    -e LLM_API_BASE_URL="http://vllm:8000/v1" \
    -e LLM_API_KEY="${VLLM_KEY}" \
    -e LLM_SERVED_NAME="ark-llm" \
    -e ARK_MODELS_DIR="/models" \
    -e TEI_EMBED_URL="http://tei-embed:80" \
    -e TEI_RERANK_URL="http://tei-rerank:80" \
    -e TIKA_URL="http://tika:9998" \
    -e OPENWEBUI_INTERNAL_URL="http://open-webui:8080" \
    -e KEYCLOAK_INTERNAL_URL="http://keycloak:8080/auth" \
    -e ARK_BACKUP_DIR="/backups" \
    -e ARK_AUDIT_LOG_FILE="/var/log/ark/audit.jsonl" \
    -e ARK_PROXY_AUTH_SECRET="${PROXY_AUTH_SECRET}" \
    -e ARK_PLUGINS_DIR="/plugins" \
    -e ARK_INGEST_SCRIPTS_DIR="/ingest-scripts" \
    -v "${TMP_ROOT}/backups:/backups" \
    -v "${TMP_ROOT}/audit:/var/log/ark" \
    -v "${TMP_ROOT}/models:/models" \
    -v "${TMP_ROOT}/plugins:/plugins:ro" \
    -v "${TMP_ROOT}/ingest-scripts:/ingest-scripts:ro" \
    ark-admin:latest >/dev/null
  docker network connect --alias ark-admin-control "${CONTROL_NET}" ark-test-ark-admin >/dev/null

  log "Starting Caddy reverse proxy on 127.0.0.1:8443..."
  docker run -d --name ark-test-caddy --network "${NET_NAME}" --network-alias caddy \
    -p 127.0.0.1:8443:443 \
    -e ARK_DOMAIN="localhost" \
    -e ARK_PROXY_AUTH_SECRET="${PROXY_AUTH_SECRET}" \
    -v "${REPO_ROOT}/config/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
    caddy:2-alpine >/dev/null
  docker network connect "${CONTROL_NET}" ark-test-caddy >/dev/null

  log "Waiting for ark-admin readiness..."
  for _ in $(seq 1 30); do
    if docker exec ark-test-ark-admin python3 -c "import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://localhost:8090/admin-console/api/version', timeout=2)
except urllib.error.HTTPError:
    pass" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  # 1. Test Keycloak OIDC Discovery through Caddy
  log "1/8 Testing Keycloak OIDC discovery via Caddy..."
  curl -ksf "https://localhost:8443/auth/realms/ark/.well-known/openid-configuration" | grep -q '"issuer"' || \
    fail "Keycloak OIDC discovery failed through Caddy."
  pass "Keycloak OIDC discovery reachable via Caddy."

  # 2. Test Admin Console SSO protection & peer-container bypass rejection
  log "2/8 Testing /admin-console/ SSO forward_auth & peer bypass rejection..."
  local admin_code spoof_caddy_code peer_bypass_code non_admin_code
  admin_code=$(curl -ks -o /dev/null -w "%{http_code}" "https://localhost:8443/admin-console/")
  [[ "${admin_code}" == "302" ]] || fail "Expected 302 redirect for unauthenticated /admin-console/, got ${admin_code}"
  spoof_caddy_code=$(curl -ks -o /dev/null -w "%{http_code}" \
    -H "X-Ark-Proxy-Secret: ${PROXY_AUTH_SECRET}" \
    -H "X-Forwarded-User: attacker" \
    -H "X-Forwarded-Groups: ark-admins" \
    "https://localhost:8443/admin-console/api/projects")
  [[ "${spoof_caddy_code}" == "302" ]] || fail "Expected 302 for forged proxy headers at Caddy, got ${spoof_caddy_code}"

  peer_bypass_code=$(docker exec ark-test-vllm python3 -c "
import urllib.request, urllib.error
req = urllib.request.Request(
    'http://ark-admin:8090/admin-console/api/projects',
    headers={'X-Forwarded-User': 'attacker', 'X-Forwarded-Groups': 'ark-admins'}
)
try:
    urllib.request.urlopen(req, timeout=5)
    print(200)
except urllib.error.HTTPError as e:
    print(e.code)
")
  [[ "${peer_bypass_code}" == "403" ]] || fail "Expected 403 for peer-container bypass to ark-admin, got ${peer_bypass_code}"

  non_admin_code=$(docker exec ark-test-ark-admin python3 -c "
import requests
r = requests.get('http://localhost:8090/admin-console/api/projects', headers={
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'jdoe',
    'X-Forwarded-Groups': 'project-alpha',
}, timeout=5)
print(r.status_code)
")
  [[ "${non_admin_code}" == "403" ]] || fail "Expected 403 for non-admin user on /admin-console/api/projects, got ${non_admin_code}"

  docker exec ark-test-ark-admin python3 -c "
import requests
H_ADMIN = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
H_USER = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'jdoe',
    'X-Forwarded-Groups': 'project-alpha',
}
v_admin = requests.get('http://localhost:8090/admin-console/api/version', headers=H_ADMIN, timeout=5)
v_admin.raise_for_status()
va = v_admin.json()
assert va['version'] == '${ARK_VERSION}', va
assert va['license'] == 'Apache-2.0', va
assert va['scheme'] == 'CalVer', va

v_portal = requests.get('http://localhost:8090/portal/api/version', headers=H_USER, timeout=5)
v_portal.raise_for_status()
assert v_portal.json()['version'] == '${ARK_VERSION}', v_portal.json()

h = requests.get('http://localhost:8090/admin-console/api/health', headers=H_ADMIN, timeout=10)
h.raise_for_status()
assert h.json()['version'] == '${ARK_VERSION}', h.json()
assert h.json()['services'].get('llm_backend') == 'healthy', h.json()
"
  pass "Admin SSO forward_auth, peer bypass block (403), ark-admins RBAC, and version endpoints verified."

  # 3. Test raw /v1 bypass blocking
  log "3/8 Testing raw /v1/* bypass block..."
  local raw_code
  raw_code=$(curl -ks -o /dev/null -w "%{http_code}" "https://localhost:8443/v1/chat/completions")
  [[ "${raw_code}" == "403" ]] || fail "Expected 403 for raw /v1/*, got ${raw_code}"
  pass "Raw /v1/* bypass blocked (HTTP 403)."

  # 4. Create RAG project + Clean project via ark-admin
  log "4/8 Provisioning RAG project (Project_Alpha) and Clean project (Clean_Sandbox)..."
  docker exec ark-test-ark-admin python3 -c "
import requests
H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
r1 = requests.post('http://localhost:8090/admin-console/api/projects', headers=H, json={'name': 'Project_Alpha', 'rag_enabled': True})
r1.raise_for_status()
r2 = requests.post('http://localhost:8090/admin-console/api/projects', headers=H, json={'name': 'Clean_Sandbox', 'rag_enabled': False})
r2.raise_for_status()
"
  pass "Created Project_Alpha (RAG) and Clean_Sandbox (Clean)."

  # 5. Issue API keys & ingest a sample document vector into Project_Alpha
  log "5/8 Issuing scoped API keys and testing Qdrant RAG + Clean project routing..."
  local keys_json
  keys_json=$(docker exec ark-test-ark-admin python3 -c "
import json, requests
from qdrant_client import QdrantClient
from qdrant_client.http.models import PointStruct
from connectors import embed_texts

H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
k1 = requests.post('http://localhost:8090/admin-console/api/keys', headers=H, json={'project_slug': 'project-alpha', 'label': 'office-addin'}).json()['api_key']
k2 = requests.post('http://localhost:8090/admin-console/api/keys', headers=H, json={'project_slug': 'clean-sandbox', 'label': 'ide-plugin'}).json()['api_key']

vec = embed_texts('http://tei-embed:80', ['ARK Project Alpha secret specification'])[0]
qc = QdrantClient(url='http://qdrant:6333', api_key='${QDRANT_KEY}')
qc.upsert(collection_name='Project_Alpha', points=[PointStruct(id='11111111-1111-1111-1111-111111111111', vector=vec, payload={'text': 'ARK Project Alpha spec v1', 'filename': 'spec.pdf'})])
print(json.dumps({'alpha_key': k1, 'clean_key': k2}))
")
  local alpha_key clean_key
  alpha_key=$(echo "${keys_json}" | python3 -c "import sys,json; print(json.load(sys.stdin)['alpha_key'])")
  clean_key=$(echo "${keys_json}" | python3 -c "import sys,json; print(json.load(sys.stdin)['clean_key'])")

  local alpha_resp clean_resp cross_code
  alpha_resp=$(curl -ks -w "\nHTTP_CODE:%{http_code}" -X POST "https://localhost:8443/projects/project-alpha/v1/chat/completions" \
    -H "Authorization: Bearer ${alpha_key}" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"Summarize spec"}]}')
  if ! echo "${alpha_resp}" | grep -q "RAG_CONTEXT_INJECTED=True"; then
    docker logs --tail 30 ark-test-caddy >&2 || true
    docker logs --tail 30 ark-test-ark-admin >&2 || true
    fail "Expected RAG_CONTEXT_INJECTED=True, got: ${alpha_resp}"
  fi
  pass "Project_Alpha API Gateway retrieved Qdrant context and returned mock LLM response."

  clean_resp=$(curl -ks -w "\nHTTP_CODE:%{http_code}" -X POST "https://localhost:8443/projects/clean-sandbox/v1/chat/completions" \
    -H "Authorization: Bearer ${clean_key}" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"Hello"}]}')
  if ! echo "${clean_resp}" | grep -q "RAG_CONTEXT_INJECTED=False"; then
    docker logs --tail 30 ark-test-ark-admin >&2 || true
    fail "Expected RAG_CONTEXT_INJECTED=False, got: ${clean_resp}"
  fi
  pass "Clean_Sandbox API Gateway routed directly to vLLM without RAG."

  cross_code=$(curl -ks -o /dev/null -w "%{http_code}" -X POST "https://localhost:8443/projects/project-alpha/v1/chat/completions" \
    -H "Authorization: Bearer ${clean_key}" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"Cross-project probe"}]}')
  [[ "${cross_code}" == "403" ]] || fail "Expected 403 for cross-project API key, got ${cross_code}"
  pass "Cross-project API key rejected (HTTP 403)."

  # 5b. Test SSE streaming on Project_Alpha
  local stream_resp
  stream_resp=$(curl -ks -X POST "https://localhost:8443/projects/project-alpha/v1/chat/completions" \
    -H "Authorization: Bearer ${alpha_key}" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"Stream test"}],"stream":true}')
  if ! echo "${stream_resp}" | grep -q "data: \[DONE\]"; then
    fail "Expected SSE stream ending with data: [DONE], got: ${stream_resp}"
  fi
  pass "Project_Alpha SSE streaming returned chunks and [DONE]."

  # 5c. Test API key revocation and project deletion
  docker exec ark-test-ark-admin python3 -c "
import requests
H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
keys = requests.get('http://localhost:8090/admin-console/api/keys', headers=H).json()
clean_id = next(k['id'] for k in keys if k['project_slug'] == 'clean-sandbox')
requests.delete(f'http://localhost:8090/admin-console/api/keys/{clean_id}', headers=H).raise_for_status()
"
  local revoked_code
  revoked_code=$(curl -ks -o /dev/null -w "%{http_code}" -X POST "https://localhost:8443/projects/clean-sandbox/v1/chat/completions" \
    -H "Authorization: Bearer ${clean_key}" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"After revoke"}]}')
  [[ "${revoked_code}" == "403" ]] || fail "Expected 403 for revoked API key, got ${revoked_code}"
  pass "Revoked API key rejected (HTTP 403)."

  docker exec ark-test-ark-admin python3 -c "
import requests
H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
requests.delete('http://localhost:8090/admin-console/api/projects/clean-sandbox', headers=H).raise_for_status()
"
  pass "Deleted Clean_Sandbox project via API."

  # 5d. Test Flexible Ingestion: Scoped Push Ingest API, Plugins, Pipelines & Scripts
  log "5d/8 Testing Flexible Data Ingestion (Push API, Key Scopes, Plugins, YAML Pipelines & Custom Scripts)..."
  cat <<'PYEOF' > "${TMP_ROOT}/ingest-scripts/test_custom.py"
import os
from ark_ingest import Document, Checkpoint, run

def sync(ctx):
    # Verify control-plane secrets are scrubbed from subprocess env
    assert "ARK_ADMIN_DB_URL" not in os.environ, "ARK_ADMIN_DB_URL leaked to script!"
    assert "QDRANT_API_KEY" not in os.environ, "QDRANT_API_KEY leaked to script!"
    assert "ARK_PROXY_AUTH_SECRET" not in os.environ, "ARK_PROXY_AUTH_SECRET leaked to script!"
    yield Document(
        doc_id="script/item-1",
        filename="item-1.md",
        content="# Custom Script Doc\nIngested from custom Python script.",
        metadata={"origin": "custom_script"},
    )
    yield Checkpoint({"last_id": 1})

if __name__ == "__main__":
    run(sync)
PYEOF

  local ingest_key
  ingest_key=$(docker exec ark-test-ark-admin python3 -c "
import requests
H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
r = requests.post('http://localhost:8090/admin-console/api/keys', headers=H, json={
    'project_slug': 'project-alpha', 'label': 'etl-ingest', 'scopes': ['ingest']
})
r.raise_for_status()
print(r.json()['api_key'])
")

  # Chat-only key must be rejected (403) on /ingest/documents; ingest-only key must be rejected (403) on /chat/completions
  local chat_on_ingest_code ingest_on_chat_code
  chat_on_ingest_code=$(curl -ks -o /dev/null -w "%{http_code}" -X POST "https://localhost:8443/projects/project-alpha/v1/ingest/documents" \
    -H "Authorization: Bearer ${alpha_key}" \
    -H "Content-Type: application/json" \
    -d '{"documents":[{"doc_id":"blocked","content":"nope"}]}')
  [[ "${chat_on_ingest_code}" == "403" ]] || fail "Expected 403 for chat-only key on /ingest/documents, got ${chat_on_ingest_code}"

  ingest_on_chat_code=$(curl -ks -o /dev/null -w "%{http_code}" -X POST "https://localhost:8443/projects/project-alpha/v1/chat/completions" \
    -H "Authorization: Bearer ${ingest_key}" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"Should fail"}]}')
  [[ "${ingest_on_chat_code}" == "403" ]] || fail "Expected 403 for ingest-only key on /chat/completions, got ${ingest_on_chat_code}"

  # Push documents -> dedup skip -> shrink doc -> delete doc
  local push_resp1 push_resp2
  push_resp1=$(curl -ksf -X POST "https://localhost:8443/projects/project-alpha/v1/ingest/documents" \
    -H "Authorization: Bearer ${ingest_key}" \
    -H "Content-Type: application/json" \
    -d '{"documents":[{"doc_id":"push/guide-1","filename":"guide.md","content":"# ARK Guide\nAir-gapped retrieval works offline.","metadata":{"doc_id":"spoofed","dept":"eng"}}]}')
  echo "${push_resp1}" | grep -q '"docs_upserted":1' || fail "Expected docs_upserted:1, got: ${push_resp1}"

  push_resp2=$(curl -ksf -X POST "https://localhost:8443/projects/project-alpha/v1/ingest/documents" \
    -H "Authorization: Bearer ${ingest_key}" \
    -H "Content-Type: application/json" \
    -d '{"documents":[{"doc_id":"push/guide-1","filename":"guide.md","content":"# ARK Guide\nAir-gapped retrieval works offline.","metadata":{"doc_id":"spoofed","dept":"eng"}}]}')
  echo "${push_resp2}" | grep -q '"docs_skipped":1' || fail "Expected docs_skipped:1 on identical re-push, got: ${push_resp2}"

  curl -ksf -X DELETE "https://localhost:8443/projects/project-alpha/v1/ingest/documents/push/guide-1" \
    -H "Authorization: Bearer ${ingest_key}" | grep -q '"status":"deleted"' || fail "Push delete failed."

  # Test Plugin discovery, YAML Pipeline Dry-Run & Sync, and Custom Script Sync
  docker exec ark-test-ark-admin python3 -c "
import requests
from qdrant_client import QdrantClient

H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
pm = requests.get('http://localhost:8090/admin-console/api/plugins', headers=H, timeout=10)
pm.raise_for_status()
pids = {p['id'] for p in pm.json()['plugins']}
assert {'smb_folder', 'sharepoint', 'mfiles', 'http_json'}.issubset(pids), pids
assert 'test_custom.py' in pm.json()['scripts'], pm.json()['scripts']

pipe_yaml = '''apiVersion: ark-ingest/v1
name: csv-test
sync_mode: full_mirror
source:
  type: csv
  data: |
    id,title,body
    r1,First Row,Hello from CSV pipeline
    r2,Second Row,Second CSV record
transform:
  doc_id: \"csv/{{ item.id }}\"
  filename: \"{{ item.title | slugify }}.md\"
  template: |
    # {{ item.title }}
    {{ item.body }}
'''
dr = requests.post('http://localhost:8090/admin-console/api/ingest-sources/dry-run', headers=H, json={
    'source_kind': 'pipeline_yaml', 'pipeline_spec': pipe_yaml, 'limit': 5
}, timeout=10)
dr.raise_for_status()
assert dr.json()['preview_count'] == 2, dr.json()

src_pipe = requests.post('http://localhost:8090/admin-console/api/ingest-sources', headers=H, json={
    'project_slug': 'project-alpha', 'name': 'CSV Pipeline', 'source_kind': 'pipeline_yaml',
    'pipeline_spec': pipe_yaml, 'sync_mode': 'full_mirror'
})
src_pipe.raise_for_status()
sid_pipe = src_pipe.json()['id']

sync_pipe = requests.post(f'http://localhost:8090/admin-console/api/ingest-sources/{sid_pipe}/sync?async_mode=false', headers=H, timeout=30)
sync_pipe.raise_for_status()
assert sync_pipe.json()['docs_upserted'] == 2, sync_pipe.json()

src_script = requests.post('http://localhost:8090/admin-console/api/ingest-sources', headers=H, json={
    'project_slug': 'project-alpha', 'name': 'Custom Script', 'source_kind': 'script',
    'script_name': 'test_custom.py'
})
src_script.raise_for_status()
sid_script = src_script.json()['id']

sync_script = requests.post(f'http://localhost:8090/admin-console/api/ingest-sources/{sid_script}/sync?async_mode=false', headers=H, timeout=30)
sync_script.raise_for_status()
assert sync_script.json()['docs_upserted'] == 1, sync_script.json()

# Clean up test ingest sources + vectors so backup/restore test sees deterministic state
requests.delete(f'http://localhost:8090/admin-console/api/ingest-sources/{sid_pipe}?delete_vectors=true', headers=H).raise_for_status()
requests.delete(f'http://localhost:8090/admin-console/api/ingest-sources/{sid_script}?delete_vectors=true', headers=H).raise_for_status()
qc = QdrantClient(url='http://qdrant:6333', api_key='${QDRANT_KEY}')
assert qc.count(collection_name='Project_Alpha').count == 1, 'Expected only original spec point remaining'
"
  pass "Push Ingest API, key scopes, plugins, YAML pipelines, and sandboxed custom scripts verified."

  # 6. Test LLM & Model Management APIs
  log "6/8 Testing LLM & Model Management APIs..."
  docker exec ark-test-ark-admin python3 -c "
import requests
H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
m = requests.get('http://localhost:8090/admin-console/api/models', headers=H, timeout=10)
m.raise_for_status()
info = m.json()
assert info['llm_live']['reachable'] is True, info
assert info['vllm_live']['reachable'] is True, info
assert 'ark-llm' in info['llm_live']['models'], info
assert info['configured']['llm_api_base_url'] == 'http://vllm:8000/v1', info
dirs = {x['dir'] for x in info['local_models']}
assert 'Qwen--Qwen2.5-14B-Instruct' in dirs and 'Qwen--Qwen2.5-32B-Instruct' in dirs, dirs

r_def = requests.post('http://localhost:8090/admin-console/api/models/default', headers=H, json={'model_name': 'ark-llm-v2'})
r_def.raise_for_status()
assert r_def.json()['default_llm_model'] == 'ark-llm-v2'
requests.post('http://localhost:8090/admin-console/api/models/default', headers=H, json={'model_name': 'ark-llm'}).raise_for_status()

r_proj = requests.patch('http://localhost:8090/admin-console/api/projects/project-alpha/model', headers=H, json={'llm_model': 'ark-llm-custom'})
r_proj.raise_for_status()
assert r_proj.json()['effective_llm_model'] == 'ark-llm-custom'
requests.patch('http://localhost:8090/admin-console/api/projects/project-alpha/model', headers=H, json={'llm_model': ''}).raise_for_status()

r_plan = requests.post('http://localhost:8090/admin-console/api/models/switch-plan', headers=H, json={'model_dir': 'Qwen--Qwen2.5-32B-Instruct', 'model_type': 'llm'})
r_plan.raise_for_status()
assert './scripts/update-llm.sh --type llm --model-dir Qwen--Qwen2.5-32B-Instruct' in r_plan.json()['command']

# Verify runtime LLM backend switching (ark_llm <-> third_party)
b_get = requests.get('http://localhost:8090/admin-console/api/llm-backend', headers=H)
b_get.raise_for_status()
assert b_get.json()['backend_type'] == 'ark_llm', b_get.json()
assert b_get.json()['backend_admin_url'] == 'http://localhost:8095', b_get.json()

b_tp = requests.post('http://localhost:8090/admin-console/api/llm-backend', headers=H, json={
    'backend_type': 'third_party',
    'api_base_url': 'http://vllm:8000/v1',
    'backend_admin_url': 'http://should-be-cleared:8095',
})
b_tp.raise_for_status()
assert b_tp.json()['backend_type'] == 'third_party', b_tp.json()
assert b_tp.json()['backend_admin_url'] == '', b_tp.json()

b_restore = requests.post('http://localhost:8090/admin-console/api/llm-backend', headers=H, json={
    'backend_type': 'ark_llm',
    'api_base_url': 'http://vllm:8000/v1',
    'backend_admin_url': 'http://localhost:8095',
})
b_restore.raise_for_status()
assert b_restore.json()['backend_admin_url'] == 'http://localhost:8095', b_restore.json()
"
  pass "LLM & Model Management endpoints verified."

  # 7. Test Full Backup (Postgres + Qdrant snapshot files) & Full-Stack Restore
  log "7/8 Testing full PostgreSQL + Qdrant backup & restore..."
  docker exec ark-test-ark-admin python3 -c "
from pathlib import Path
import requests
from qdrant_client import QdrantClient
from backup_utils import verify_backup_manifest

H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
r = requests.post('http://localhost:8090/admin-console/api/backups/run', headers=H, timeout=60)
if not r.ok:
    raise RuntimeError(f'Backup failed HTTP {r.status_code}: {r.text}')
data = r.json()
assert len(data['databases']) == 3, data
assert len(data['qdrant_snapshots']) >= 2, data

dest = Path(data['destination'])
assert (dest / 'manifest.json').is_file(), 'manifest.json missing'
manifest = verify_backup_manifest(dest)
assert manifest.get('ark_version') == '${ARK_VERSION}', manifest
assert len(manifest['databases']) == 3, manifest
assert len(manifest['qdrant_snapshots']) >= 2, manifest
for s in manifest['qdrant_snapshots']:
    snap_path = dest / s['path']
    assert snap_path.is_file() and snap_path.stat().st_size > 0, f'Missing snapshot file: {snap_path}'

# Mutate Qdrant + DB state, then verify restore recovers both
qc = QdrantClient(url='http://qdrant:6333', api_key='${QDRANT_KEY}')
qc.delete(collection_name='Project_Alpha', points_selector=['11111111-1111-1111-1111-111111111111'])
assert qc.count(collection_name='Project_Alpha').count == 0
requests.patch('http://localhost:8090/admin-console/api/projects/project-alpha/model', headers=H, json={'llm_model': 'mutated-before-restore'}).raise_for_status()

rest = requests.post('http://localhost:8090/admin-console/api/backups/restore', headers=H, json={'backup_name': data['backup_name']}, timeout=120)
if not rest.ok:
    raise RuntimeError(f'Restore failed HTTP {rest.status_code}: {rest.text}')
rdata = rest.json()
assert set(rdata['restored_databases']) == {'openwebui', 'keycloak', 'ark_admin'}, rdata
assert 'Project_Alpha' in rdata['restored_collections'], rdata
assert qc.count(collection_name='Project_Alpha').count == 1, 'Qdrant point was not restored!'
proj_info = [p for p in requests.get('http://localhost:8090/admin-console/api/projects', headers=H).json() if p['slug'] == 'project-alpha'][0]
assert proj_info['llm_model'] == '', proj_info
"
  pass "Backup files, Qdrant snapshot download, manifest verification, and full restore verified."

  # 8. Test Audit Logs (PostgreSQL + append-only audit.jsonl)
  log "8/8 Verifying audit logs..."
  docker exec ark-test-ark-admin python3 -c "
import json, requests
from pathlib import Path
H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
logs = requests.get('http://localhost:8090/admin-console/api/audit?limit=40', headers=H).json()
events = {l['event_type'] for l in logs}
for expected in (
    'auth.denied',
    'project.create',
    'project.delete',
    'project.model_update',
    'model.default_update',
    'model.switch_plan',
    'apikey.create',
    'apikey.revoke',
    'api.chat_completion',
    'api.auth_failed',
    'api.scope_denied',
    'ingest.push',
    'ingest.delete',
    'ingest.dry_run',
    'ingest.job_start',
    'ingest.job_finish',
    'backup.restore',
):
    assert expected in events, f'{expected} missing from DB audit_logs: {events}'

jsonl_events = {
    json.loads(line)['event_type']
    for line in Path('/var/log/ark/audit.jsonl').read_text(encoding='utf-8').splitlines()
    if line.strip()
}
assert 'backup.run' in jsonl_events, jsonl_events
assert 'backup.restore' in jsonl_events, jsonl_events
assert 'auth.denied' in jsonl_events, jsonl_events
"
  pass "All expected events recorded in PostgreSQL audit_logs and audit.jsonl."

  log "ALL INTEGRATION TESTS PASSED!"
}

main "$@"
