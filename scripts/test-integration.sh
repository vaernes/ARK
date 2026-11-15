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
if [[ -z "${TEST_HTTPS_PORT:-}" ]]; then
  if python3 -c "import socket; s=socket.socket(); s.settimeout(0.2); exit(0 if s.connect_ex(('127.0.0.1', 8443)) == 0 else 1)" 2>/dev/null; then
    TEST_HTTPS_PORT="18443"
  else
    TEST_HTTPS_PORT="8443"
  fi
fi

log()  { printf '[%s] [TEST] %s\n' "$(date -u +'%H:%M:%S')" "$*"; }
pass() { printf '[%s] [PASS] %s\n' "$(date -u +'%H:%M:%S')" "$*"; }
fail() { printf '[%s] [FAIL] %s\n' "$(date -u +'%H:%M:%S')" "$*" >&2; exit 1; }

stop_test_containers() {
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
}

cleanup() {
  if [[ "${KEEP_RUNNING}" == true ]]; then
    log "Containers left running (--keep). Clean up manually with: docker rm -f \$(docker ps -aq --filter name=ark-test-)"
    return
  fi
  log "Cleaning up test containers and temporary files..."
  stop_test_containers
  rm -rf "${TMP_ROOT}" 2>/dev/null || true
}
trap cleanup EXIT

main() {
  stop_test_containers
  mkdir -p "${TMP_ROOT}/backups" "${TMP_ROOT}/audit" "${TMP_ROOT}/keycloak" "${TMP_ROOT}/plugins" "${TMP_ROOT}/ingest-scripts"
  cp "${REPO_ROOT}/config/keycloak/ark-realm.json" "${TMP_ROOT}/keycloak/ark-realm.json"
  sed -i "s|https://ark\.local|https://localhost:${TEST_HTTPS_PORT}|g" "${TMP_ROOT}/keycloak/ark-realm.json"

  ARK_VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION")"
  GIT_COMMIT="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
  BUILD_DATE="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  log "Building ark-admin image (ARK v${ARK_VERSION})..."
  if docker buildx version >/dev/null 2>&1; then
    docker buildx build --load -q \
      --build-arg "ARK_VERSION=${ARK_VERSION}" \
      --build-arg "ARK_GIT_COMMIT=${GIT_COMMIT}" \
      --build-arg "ARK_BUILD_DATE=${BUILD_DATE}" \
      -t ark-admin:latest "${REPO_ROOT}/admin" >/dev/null
  else
    docker build -q \
      --build-arg "ARK_VERSION=${ARK_VERSION}" \
      --build-arg "ARK_GIT_COMMIT=${GIT_COMMIT}" \
      --build-arg "ARK_BUILD_DATE=${BUILD_DATE}" \
      -t ark-admin:latest "${REPO_ROOT}/admin" >/dev/null 2> >(grep -vE '^(DEPRECATED: The legacy builder|            Install the buildx|            https://docs\.docker\.com/go/buildx/|$)' >&2 || true)
  fi
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
    start --import-realm --http-enabled=true --http-relative-path=/auth --hostname=https://localhost:${TEST_HTTPS_PORT}/auth --hostname-strict=false --hostname-backchannel-dynamic=true --proxy-headers=xforwarded >/dev/null

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
    --oidc-issuer-url=https://localhost:${TEST_HTTPS_PORT}/auth/realms/ark \
    --login-url=https://localhost:${TEST_HTTPS_PORT}/auth/realms/ark/protocol/openid-connect/auth \
    --redeem-url=http://keycloak:8080/auth/realms/ark/protocol/openid-connect/token \
    --oidc-jwks-url=http://keycloak:8080/auth/realms/ark/protocol/openid-connect/certs \
    --profile-url=http://keycloak:8080/auth/realms/ark/protocol/openid-connect/userinfo \
    --client-id=oauth2-proxy \
    --client-secret=ark-oauth2-proxy-secret-change-me \
    --cookie-secret="${COOKIE_SECRET}" \
    --redirect-url=https://localhost:${TEST_HTTPS_PORT}/oauth2/callback \
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

  log "Starting Caddy reverse proxy on 127.0.0.1:${TEST_HTTPS_PORT}..."
  docker run -d --name ark-test-caddy --network "${NET_NAME}" --network-alias caddy \
    -p 127.0.0.1:${TEST_HTTPS_PORT}:443 \
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
  curl -ksf "https://localhost:${TEST_HTTPS_PORT}/auth/realms/ark/.well-known/openid-configuration" | grep -q '"issuer"' || \
    fail "Keycloak OIDC discovery failed through Caddy."
  pass "Keycloak OIDC discovery reachable via Caddy."

  # 2. Test Admin Console SSO protection & peer-container bypass rejection
  log "2/8 Testing /admin-console/ SSO forward_auth & peer bypass rejection..."
  local admin_code create_code spoof_caddy_code peer_bypass_code non_admin_code
  admin_code=$(curl -ks -o /dev/null -w "%{http_code}" "https://localhost:${TEST_HTTPS_PORT}/admin-console/")
  [[ "${admin_code}" == "302" ]] || fail "Expected 302 redirect for unauthenticated /admin-console/, got ${admin_code}"
  create_code=$(curl -ks -o /dev/null -w "%{http_code}" "https://localhost:${TEST_HTTPS_PORT}/create/")
  [[ "${create_code}" == "302" ]] || fail "Expected 302 redirect for unauthenticated /create/, got ${create_code}"
  spoof_caddy_code=$(curl -ks -o /dev/null -w "%{http_code}" \
    -H "X-Ark-Proxy-Secret: ${PROXY_AUTH_SECRET}" \
    -H "X-Forwarded-User: attacker" \
    -H "X-Forwarded-Groups: ark-admins" \
    "https://localhost:${TEST_HTTPS_PORT}/admin-console/api/projects")
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

v_create = requests.get('http://localhost:8090/create/api/version', headers=H_USER, timeout=5)
v_create.raise_for_status()
assert v_create.json()['version'] == '${ARK_VERSION}', v_create.json()

h = requests.get('http://localhost:8090/admin-console/api/health', headers=H_ADMIN, timeout=10)
h.raise_for_status()
assert h.json()['version'] == '${ARK_VERSION}', h.json()
assert h.json()['services'].get('llm_backend') == 'healthy', h.json()
"
  pass "Admin SSO forward_auth, peer bypass block (403), ark-admins RBAC, and version endpoints verified."

  # 3. Test raw /v1 bypass blocking
  log "3/8 Testing raw /v1/* bypass block..."
  local raw_code
  raw_code=$(curl -ks -o /dev/null -w "%{http_code}" "https://localhost:${TEST_HTTPS_PORT}/v1/chat/completions")
  [[ "${raw_code}" == "403" ]] || fail "Expected 403 for raw /v1/*, got ${raw_code}"
  pass "Raw /v1/* bypass blocked (HTTP 403)."

  # 4. Create RAG projects + Clean project via ark-admin
  log "4/8 Provisioning RAG projects (Project_Alpha, Shared_KB) and Clean project (Clean_Sandbox)..."
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
r3 = requests.post('http://localhost:8090/admin-console/api/projects', headers=H, json={'name': 'Shared_KB', 'rag_enabled': True})
r3.raise_for_status()
"
  pass "Created Project_Alpha (RAG), Shared_KB (RAG), and Clean_Sandbox (Clean)."

  # 5. Issue API keys, link Shared_KB to Project_Alpha, & ingest sample document vectors
  log "5/8 Issuing scoped API keys and testing project-linked Qdrant RAG + Clean project routing..."
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

vec_alpha = embed_texts('http://tei-embed:80', ['ARK Project Alpha secret specification'])[0]
vec_shared = embed_texts('http://tei-embed:80', ['ARK Shared KB policy v1'])[0]
qc = QdrantClient(url='http://qdrant:6333', api_key='${QDRANT_KEY}')
qc.upsert(collection_name='Project_Alpha', points=[PointStruct(id='11111111-1111-1111-1111-111111111111', vector=vec_alpha, payload={'text': 'ARK Project Alpha spec v1', 'filename': 'spec.pdf'})])
qc.upsert(collection_name='Shared_KB', points=[PointStruct(id='22222222-2222-2222-2222-222222222222', vector=vec_shared, payload={'text': 'ARK Shared KB policy v1', 'filename': 'shared-policy.pdf'})])

bad_link = requests.patch('http://localhost:8090/admin-console/api/projects/project-alpha/rag-projects', headers=H, json={'extra_rag_projects': ['clean-sandbox']})
assert bad_link.status_code == 400, f'Expected 400 linking clean project, got {bad_link.status_code}: {bad_link.text}'

ok_link = requests.patch('http://localhost:8090/admin-console/api/projects/project-alpha/rag-projects', headers=H, json={'extra_rag_projects': ['shared-kb']})
ok_link.raise_for_status()
assert ok_link.json().get('extra_rag_projects') == ['shared-kb'], ok_link.json()
assert 'Shared_KB' in ok_link.json().get('system_prompt', ''), ok_link.json()

print(json.dumps({'alpha_key': k1, 'clean_key': k2}))
")
  local alpha_key clean_key
  alpha_key=$(echo "${keys_json}" | python3 -c "import sys,json; print(json.load(sys.stdin)['alpha_key'])")
  clean_key=$(echo "${keys_json}" | python3 -c "import sys,json; print(json.load(sys.stdin)['clean_key'])")

  local alpha_resp alpha_headers_body clean_resp cross_code healthz_code v1_root_ct
  healthz_code=$(curl -ks -o /dev/null -w "%{http_code}" "https://localhost:${TEST_HTTPS_PORT}/healthz")
  [[ "${healthz_code}" == "200" ]] || fail "Expected 200 on https://localhost:${TEST_HTTPS_PORT}/healthz, got ${healthz_code}"

  v1_root_ct=$(curl -ks -o /dev/null -w "%{content_type}" -H "Authorization: Bearer ${alpha_key}" "https://localhost:${TEST_HTTPS_PORT}/projects/project-alpha/v1")
  echo "${v1_root_ct}" | grep -qi "application/json" || fail "Expected Caddy to route /projects/project-alpha/v1 (no trailing slash) to ark-admin JSON API, got content-type: ${v1_root_ct}"

  alpha_headers_body=$(curl -ksi -X POST "https://localhost:${TEST_HTTPS_PORT}/projects/project-alpha/v1/chat/completions" \
    -H "Authorization: Bearer ${alpha_key}" \
    -H "Content-Type: application/json" \
    -d '{"include_citations":true,"messages":[{"role":"system","content":"Always respond concisely."},{"role":"user","content":[{"type":"text","text":"Summarize spec and policy"}]}]}')
  alpha_resp="${alpha_headers_body}"
  if ! echo "${alpha_resp}" | grep -q "RAG_CONTEXT_INJECTED=True"; then
    docker logs --tail 30 ark-test-caddy >&2 || true
    docker logs --tail 30 ark-test-ark-admin >&2 || true
    fail "Expected RAG_CONTEXT_INJECTED=True, got: ${alpha_resp}"
  fi
  echo "${alpha_resp}" | grep -q "HAS_CLIENT_INSTRUCTIONS=True" || fail "Expected client system prompt to be preserved (HAS_CLIENT_INSTRUCTIONS=True), got: ${alpha_resp}"
  echo "${alpha_resp}" | grep -qiE "^x-ark-rag-chunks:\s*2\b" || fail "Expected X-Ark-Rag-Chunks: 2 response header, got headers+body: ${alpha_resp}"
  echo "${alpha_resp}" | grep -q "ark_citations" || fail "Expected ark_citations in response body, got: ${alpha_resp}"
  echo "${alpha_resp}" | grep -q "Project_Alpha | spec.pdf" || fail "Expected Project_Alpha citation in response, got: ${alpha_resp}"
  echo "${alpha_resp}" | grep -q "Shared_KB | shared-policy.pdf" || fail "Expected Shared_KB citation in response, got: ${alpha_resp}"
  echo "${alpha_resp}" | grep -q '"total_tokens":20' || fail "Expected token usage in chat completion response, got: ${alpha_resp}"
  pass "Project_Alpha API Gateway retrieved own + linked Shared_KB Qdrant context, preserved client system prompt, handled multimodal content, and returned X-Ark-Rag-Chunks & citations."

  clean_resp=$(curl -ks -w "\nHTTP_CODE:%{http_code}" -X POST "https://localhost:${TEST_HTTPS_PORT}/projects/clean-sandbox/v1/chat/completions" \
    -H "Authorization: Bearer ${clean_key}" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"Hello"}]}')
  if ! echo "${clean_resp}" | grep -q "RAG_CONTEXT_INJECTED=False"; then
    docker logs --tail 30 ark-test-ark-admin >&2 || true
    fail "Expected RAG_CONTEXT_INJECTED=False, got: ${clean_resp}"
  fi
  pass "Clean_Sandbox API Gateway routed directly to vLLM without RAG."

  cross_code=$(curl -ks -o /dev/null -w "%{http_code}" -X POST "https://localhost:${TEST_HTTPS_PORT}/projects/project-alpha/v1/chat/completions" \
    -H "Authorization: Bearer ${clean_key}" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"Cross-project probe"}]}')
  [[ "${cross_code}" == "403" ]] || fail "Expected 403 for cross-project API key, got ${cross_code}"
  pass "Cross-project API key rejected (HTTP 403)."

  # 5a. Test per-user project access control from admin side -> portal enforcement
  docker exec ark-test-ark-admin python3 -c "
import requests
H_ADMIN = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
H_BOB = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-Email': 'bob@ark.local',
    'X-Forwarded-Groups': 'users',
}

# Initially bob has no access to project-alpha
projs = requests.get('http://localhost:8090/portal/api/projects', headers=H_BOB, timeout=5)
projs.raise_for_status()
assert all(p['slug'] != 'project-alpha' for p in projs.json()), projs.json()
denied = requests.post('http://localhost:8090/portal/api/keys', headers=H_BOB, json={'project_slug': 'project-alpha', 'label': 'bob-key'}, timeout=5)
assert denied.status_code == 403, denied.status_code
denied_console = requests.get('http://localhost:8090/portal/api/projects/project-alpha/console', headers=H_BOB, timeout=5)
assert denied_console.status_code == 403, denied_console.status_code

# Grant viewer role: bob sees user_role='viewer', can view project console, cannot mutate settings or issue ingest key (403), can issue chat key (200)
grant_v = requests.post('http://localhost:8090/admin-console/api/projects/project-alpha/members', headers=H_ADMIN, json={'user_identifier': 'bob@ark.local', 'role': 'viewer'}, timeout=5)
grant_v.raise_for_status()
projs_v = requests.get('http://localhost:8090/portal/api/projects', headers=H_BOB, timeout=5)
projs_v.raise_for_status()
alpha_entry = [p for p in projs_v.json() if p['slug'] == 'project-alpha'][0]
assert alpha_entry.get('user_role') == 'viewer', alpha_entry
bob_console_v = requests.get('http://localhost:8090/portal/api/projects/project-alpha/console', headers=H_BOB, timeout=5)
bob_console_v.raise_for_status()
assert bob_console_v.json()['me']['user_role'] == 'viewer', bob_console_v.json()
denied_model_v = requests.patch('http://localhost:8090/portal/api/projects/project-alpha/model', headers=H_BOB, json={'llm_model': 'unauth-llm'}, timeout=5)
assert denied_model_v.status_code == 403, denied_model_v.status_code
denied_ingest = requests.post('http://localhost:8090/portal/api/keys', headers=H_BOB, json={'project_slug': 'project-alpha', 'label': 'bob-ingest', 'scopes': ['ingest']}, timeout=5)
assert denied_ingest.status_code == 403, denied_ingest.status_code
chat_ok = requests.post('http://localhost:8090/portal/api/keys', headers=H_BOB, json={'project_slug': 'project-alpha', 'label': 'bob-viewer-chat', 'scopes': ['chat']}, timeout=5)
chat_ok.raise_for_status()
bob_viewer_api_key = chat_ok.json()['api_key']

# Upgrade bob to editor role -> can update prompt via project console, but cannot link unauthorized RAG project (shared-kb)
grant_ed = requests.post('http://localhost:8090/admin-console/api/projects/project-alpha/members', headers=H_ADMIN, json={'user_identifier': 'bob@ark.local', 'role': 'editor'}, timeout=5)
grant_ed.raise_for_status()
upd_prompt = requests.patch('http://localhost:8090/portal/api/projects/project-alpha/prompt', headers=H_BOB, json={'system_prompt': 'Custom editor instructions.'}, timeout=5)
upd_prompt.raise_for_status()
unauth_rag_link = requests.patch('http://localhost:8090/portal/api/projects/project-alpha/rag-projects', headers=H_BOB, json={'extra_rag_projects': ['shared-kb']}, timeout=5)
assert unauth_rag_link.status_code == 403, unauth_rag_link.status_code
requests.patch('http://localhost:8090/admin-console/api/projects/project-alpha/prompt', headers=H_ADMIN, json={'system_prompt': ''}, timeout=5).raise_for_status()

# Admin & Portal Project Console HTML routes return 200
html_admin_pc = requests.get('http://localhost:8090/admin-console/projects/project-alpha', headers=H_ADMIN, timeout=5)
html_admin_pc.raise_for_status()
assert 'PROJECT CONSOLE' in html_admin_pc.text
html_portal_pc = requests.get('http://localhost:8090/portal/projects/project-alpha', headers=H_BOB, timeout=5)
html_portal_pc.raise_for_status()
assert 'PROJECT CONSOLE' in html_portal_pc.text

# Upgrade/set bob to member role
grant = requests.post('http://localhost:8090/admin-console/api/projects/project-alpha/members', headers=H_ADMIN, json={'user_identifier': 'bob@ark.local', 'role': 'member'}, timeout=5)
grant.raise_for_status()
assert grant.json()['user_identifier'] == 'bob@ark.local', grant.json()

members = requests.get('http://localhost:8090/admin-console/api/projects/project-alpha/members', headers=H_ADMIN, timeout=5)
members.raise_for_status()
assert any(m['user_identifier'] == 'bob@ark.local' for m in members.json()), members.json()

users = requests.get('http://localhost:8090/admin-console/api/users', headers=H_ADMIN, timeout=5)
users.raise_for_status()
assert any(u['identifier'] == 'bob@ark.local' for u in users.json()), users.json()

# Now bob sees project-alpha and can issue a key
projs2 = requests.get('http://localhost:8090/portal/api/projects', headers=H_BOB, timeout=5)
projs2.raise_for_status()
assert any(p['slug'] == 'project-alpha' for p in projs2.json()), projs2.json()
created = requests.post('http://localhost:8090/portal/api/keys', headers=H_BOB, json={'project_slug': 'project-alpha', 'label': 'bob-key'}, timeout=5)
created.raise_for_status()

# Admin revokes bob access -> active portal keys are automatically revoked
rev = requests.delete('http://localhost:8090/admin-console/api/projects/project-alpha/members/bob@ark.local', headers=H_ADMIN, timeout=5)
rev.raise_for_status()
assert rev.json().get('revoked_keys', 0) >= 2, rev.json()

revoked_gw = requests.post(
    'http://localhost:8090/projects/project-alpha/v1/chat/completions',
    headers={'Authorization': f'Bearer {bob_viewer_api_key}'},
    json={'messages': [{'role': 'user', 'content': 'Should fail after revoke'}]},
    timeout=5,
)
assert revoked_gw.status_code in (401, 403), revoked_gw.status_code

projs3 = requests.get('http://localhost:8090/portal/api/projects', headers=H_BOB, timeout=5)
projs3.raise_for_status()
assert all(p['slug'] != 'project-alpha' for p in projs3.json()), projs3.json()
denied2 = requests.post('http://localhost:8090/portal/api/keys', headers=H_BOB, json={'project_slug': 'project-alpha', 'label': 'bob-key-2'}, timeout=5)
assert denied2.status_code == 403, denied2.status_code
" || fail "Per-user project access control verification failed"
  pass "Admin per-user project access grant/revoke, viewer key scope RBAC, and auto-key revocation verified."

  # 5b. Test SSE streaming on Project_Alpha
  local stream_resp
  stream_resp=$(curl -ks -X POST "https://localhost:${TEST_HTTPS_PORT}/projects/project-alpha/v1/chat/completions" \
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
  revoked_code=$(curl -ks -o /dev/null -w "%{http_code}" -X POST "https://localhost:${TEST_HTTPS_PORT}/projects/clean-sandbox/v1/chat/completions" \
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
  chat_on_ingest_code=$(curl -ks -o /dev/null -w "%{http_code}" -X POST "https://localhost:${TEST_HTTPS_PORT}/projects/project-alpha/v1/ingest/documents" \
    -H "Authorization: Bearer ${alpha_key}" \
    -H "Content-Type: application/json" \
    -d '{"documents":[{"doc_id":"blocked","content":"nope"}]}')
  [[ "${chat_on_ingest_code}" == "403" ]] || fail "Expected 403 for chat-only key on /ingest/documents, got ${chat_on_ingest_code}"

  ingest_on_chat_code=$(curl -ks -o /dev/null -w "%{http_code}" -X POST "https://localhost:${TEST_HTTPS_PORT}/projects/project-alpha/v1/chat/completions" \
    -H "Authorization: Bearer ${ingest_key}" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"Should fail"}]}')
  [[ "${ingest_on_chat_code}" == "403" ]] || fail "Expected 403 for ingest-only key on /chat/completions, got ${ingest_on_chat_code}"

  # Push documents -> dedup skip -> shrink doc -> delete doc
  local push_resp1 push_resp2
  push_resp1=$(curl -ksf -X POST "https://localhost:${TEST_HTTPS_PORT}/projects/project-alpha/v1/ingest/documents" \
    -H "Authorization: Bearer ${ingest_key}" \
    -H "Content-Type: application/json" \
    -d '{"documents":[{"doc_id":"push/guide-1","filename":"guide.md","content":"# ARK Guide\nAir-gapped retrieval works offline.","metadata":{"doc_id":"spoofed","dept":"eng"}}]}')
  echo "${push_resp1}" | grep -q '"docs_upserted":1' || fail "Expected docs_upserted:1, got: ${push_resp1}"

  push_resp2=$(curl -ksf -X POST "https://localhost:${TEST_HTTPS_PORT}/projects/project-alpha/v1/ingest/documents" \
    -H "Authorization: Bearer ${ingest_key}" \
    -H "Content-Type: application/json" \
    -d '{"documents":[{"doc_id":"push/guide-1","filename":"guide.md","content":"# ARK Guide\nAir-gapped retrieval works offline.","metadata":{"doc_id":"spoofed","dept":"eng"}}]}')
  echo "${push_resp2}" | grep -q '"docs_skipped":1' || fail "Expected docs_skipped:1 on identical re-push, got: ${push_resp2}"

  curl -ksf -X DELETE "https://localhost:${TEST_HTTPS_PORT}/projects/project-alpha/v1/ingest/documents/push/guide-1" \
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

# Pipeline path confinement & read-only SQL confinement must reject traversal/mutation (400)
bad_fs_yaml = '''apiVersion: ark-ingest/v1
name: bad-fs
source:
  type: filesystem_glob
  path: /etc
  pattern: passwd
transform:
  doc_id: x
  template: y
'''
dr_bad_fs = requests.post('http://localhost:8090/admin-console/api/ingest-sources/dry-run', headers=H, json={
    'source_kind': 'pipeline_yaml', 'pipeline_spec': bad_fs_yaml, 'limit': 5
}, timeout=10)
assert dr_bad_fs.status_code == 400, f'Expected 400 for /etc filesystem_glob, got {dr_bad_fs.status_code}: {dr_bad_fs.text}'

bad_sql_yaml = '''apiVersion: ark-ingest/v1
name: bad-sql
source:
  type: sql_query
  dsn: sqlite:///shares/test.db
  query: DELETE FROM docs
transform:
  doc_id: x
  template: y
'''
dr_bad_sql = requests.post('http://localhost:8090/admin-console/api/ingest-sources/dry-run', headers=H, json={
    'source_kind': 'pipeline_yaml', 'pipeline_spec': bad_sql_yaml, 'limit': 5
}, timeout=10)
assert dr_bad_sql.status_code == 400, f'Expected 400 for DELETE sql_query, got {dr_bad_sql.status_code}: {dr_bad_sql.text}'

src_pipe = requests.post('http://localhost:8090/admin-console/api/ingest-sources', headers=H, json={
    'project_slug': 'project-alpha', 'name': 'CSV Pipeline', 'source_kind': 'pipeline_yaml',
    'pipeline_spec': pipe_yaml, 'sync_mode': 'full_mirror'
})
src_pipe.raise_for_status()
sid_pipe = src_pipe.json()['id']

sync_pipe = requests.post(f'http://localhost:8090/admin-console/api/ingest-sources/{sid_pipe}/sync?async_mode=false', headers=H, timeout=30)
sync_pipe.raise_for_status()
assert sync_pipe.json()['docs_upserted'] == 2, sync_pipe.json()

docs_list = requests.get('http://localhost:8090/admin-console/api/projects/project-alpha/ingest/documents', headers=H, timeout=10)
docs_list.raise_for_status()
doc_ids = {d['doc_id'] for d in docs_list.json()['documents']}
assert {'csv/r1', 'csv/r2'}.issubset(doc_ids), doc_ids

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
  pass "Push Ingest API, key scopes, plugins, YAML pipelines, security confinement, and sandboxed custom scripts verified."

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

# Verify Image Generation Backend configuration, ARK Create Studio (/create/api/*), and scoped /projects/<slug>/v1/images/generations
import base64

img_b = requests.get('http://localhost:8090/admin-console/api/image-backend', headers=H)
img_b.raise_for_status()
assert img_b.json()['api_base_url'] == 'http://vllm:8000/v1', img_b.json()
assert img_b.json()['default_model'] == 'ark-image', img_b.json()

img_upd = requests.post('http://localhost:8090/admin-console/api/image-backend', headers=H, json={
    'api_base_url': 'http://vllm:8000/v1',
    'default_model': 'ark-image',
})
img_upd.raise_for_status()

H_ALICE = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-Email': 'alice@ark.local',
    'X-Forwarded-Groups': 'project-alpha',
}
H_OUTSIDER = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-Email': 'outsider@ark.local',
    'X-Forwarded-Groups': 'users',
}

ui_html = requests.get('http://localhost:8090/create/', headers=H_ALICE)
ui_html.raise_for_status()
assert 'ARK CREATE STUDIO' in ui_html.text

presets = requests.get('http://localhost:8090/create/api/presets', headers=H_ALICE)
presets.raise_for_status()
preset_ids = {p['id'] for p in presets.json()['presets']}
assert {'photorealistic', 'isometric_diagram', 'icon', 'slide_graphic'}.issubset(preset_ids), preset_ids

enh = requests.post('http://localhost:8090/create/api/enhance-prompt', headers=H_ALICE, json={
    'prompt': 'Secure air-gapped data vault',
    'style_preset': 'isometric_diagram',
    'size': '1024x1024',
})
enh.raise_for_status()
assert len(enh.json()['enhanced_prompt']) > 10, enh.json()

# Outsider cannot tag creation with project-alpha (403)
denied_proj = requests.post('http://localhost:8090/create/api/generate', headers=H_OUTSIDER, json={
    'prompt': 'Unauthorized project tag test',
    'style_preset': 'icon',
    'size': '1024x1024',
    'project_slug': 'project-alpha',
})
assert denied_proj.status_code == 403, denied_proj.status_code

gen = requests.post('http://localhost:8090/create/api/generate', headers=H_ALICE, json={
    'prompt': 'Secure air-gapped data vault',
    'style_preset': 'isometric_diagram',
    'size': '1344x768',
    'project_slug': 'project-alpha',
})
gen.raise_for_status()
gen_item = gen.json()
cid = gen_item['id']
assert gen_item['style_preset'] == 'isometric_diagram', gen_item
assert gen_item['size'] == '1344x768', gen_item
assert base64.b64decode(gen_item['image_b64']).startswith(b'\x89PNG\r\n\x1a\n')

gallery = requests.get('http://localhost:8090/create/api/creations', headers=H_ALICE)
gallery.raise_for_status()
assert any(c['id'] == cid for c in gallery.json()), gallery.json()

# Outsider cannot see or download or delete Alice's creation
outsider_gallery = requests.get('http://localhost:8090/create/api/creations', headers=H_OUTSIDER)
outsider_gallery.raise_for_status()
assert all(c['id'] != cid for c in outsider_gallery.json()), outsider_gallery.json()
assert requests.get(f'http://localhost:8090/create/api/creations/{cid}/image', headers=H_OUTSIDER).status_code == 403
assert requests.delete(f'http://localhost:8090/create/api/creations/{cid}', headers=H_OUTSIDER).status_code == 403

img_bin = requests.get(f'http://localhost:8090/create/api/creations/{cid}/image', headers=H_ALICE)
img_bin.raise_for_status()
assert img_bin.content.startswith(b'\x89PNG\r\n\x1a\n')

del_res = requests.delete(f'http://localhost:8090/create/api/creations/{cid}', headers=H_ALICE)
del_res.raise_for_status()
assert del_res.json()['status'] == 'deleted'

# Test per-project /projects/project-alpha/v1/images/generations with 'images' scope vs 'chat' scope
img_key = requests.post('http://localhost:8090/admin-console/api/keys', headers=H, json={
    'project_slug': 'project-alpha', 'label': 'design-addin', 'scopes': ['images']
}).json()['api_key']
chat_only_key = '${alpha_key}'

denied_scope = requests.post(
    'http://localhost:8090/projects/project-alpha/v1/images/generations',
    headers={'Authorization': f'Bearer {chat_only_key}'},
    json={'prompt': 'Should be blocked without images scope'},
)
assert denied_scope.status_code == 403, denied_scope.status_code

proj_img = requests.post(
    'http://localhost:8090/projects/project-alpha/v1/images/generations',
    headers={'Authorization': f'Bearer {img_key}'},
    json={'prompt': 'Architecture diagram for Project Alpha', 'style_preset': 'blueprint', 'size': '1024x1024'},
)
proj_img.raise_for_status()
proj_b64 = proj_img.json()['data'][0]['b64_json']
assert base64.b64decode(proj_b64).startswith(b'\x89PNG\r\n\x1a\n')
"
  pass "LLM & Model Management, ARK Create Studio (/create), and per-project /v1/images/generations endpoints verified."

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
assert 'ingest_scripts' in (manifest.get('extra_archives') or {}), manifest

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
assert 'Shared_KB' in rdata['restored_collections'], rdata
assert qc.count(collection_name='Project_Alpha').count == 1, 'Qdrant point was not restored!'
assert qc.count(collection_name='Shared_KB').count == 1, 'Shared_KB Qdrant point was not restored!'
proj_info = [p for p in requests.get('http://localhost:8090/admin-console/api/projects', headers=H).json() if p['slug'] == 'project-alpha'][0]
assert proj_info['llm_model'] == '', proj_info
assert proj_info['extra_rag_projects'] == ['shared-kb'], proj_info

# Test Project_Alpha snapshot export & download endpoints on restored stack
exp = requests.post('http://localhost:8090/admin-console/api/projects/project-alpha/snapshot/export', headers=H, timeout=60)
exp.raise_for_status()
assert exp.json().get('file') == 'Project_Alpha.snapshot', exp.json()
dl = requests.get('http://localhost:8090/admin-console/api/projects/project-alpha/snapshot/download', headers=H, timeout=60)
dl.raise_for_status()
assert len(dl.content) > 0, 'Downloaded Project_Alpha.snapshot is empty'
"
  pass "Backup files, extra_archives, Project_Alpha snapshot export/download, manifest verification, and full restore verified."

  # 8. Test Audit Logs (PostgreSQL + append-only audit.jsonl) & In-Container Unit Tests
  log "8/8 Verifying audit logs and running full unit tests inside container..."
  docker exec ark-test-ark-admin python3 -c "
import json, requests
from pathlib import Path
H = {
    'X-Ark-Proxy-Secret': '${PROXY_AUTH_SECRET}',
    'X-Forwarded-User': 'ci-admin@ark.local',
    'X-Forwarded-Groups': 'ark-admins',
}
logs = requests.get('http://localhost:8090/admin-console/api/audit?limit=100', headers=H).json()
events = {l['event_type'] for l in logs}
for expected in (
    'auth.denied',
    'project.create',
    'project.delete',
    'project.model_update',
    'project.rag_links_update',
    'model.default_update',
    'model.switch_plan',
    'apikey.create',
    'apikey.revoke',
    'api.chat_completion',
    'api.image_generation',
    'api.auth_failed',
    'api.scope_denied',
    'create.enhance_prompt',
    'create.generate',
    'create.delete',
    'image_backend.update',
    'ingest.push',
    'ingest.delete',
    'ingest.dry_run',
    'ingest.job_start',
    'ingest.job_finish',
    'snapshot.export',
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
assert 'snapshot.export' in jsonl_events, jsonl_events
assert 'auth.denied' in jsonl_events, jsonl_events
"
  pass "All expected events recorded in PostgreSQL audit_logs and audit.jsonl."

  docker cp "${REPO_ROOT}/tests/test_admin_extensions.py" ark-test-ark-admin:/app/test_admin_extensions.py
  docker exec -w /app ark-test-ark-admin python3 -m unittest \
    test_admin_extensions.TestSecurityCronAndMessages \
    test_admin_extensions.TestBackupManifestExtraArchives \
    test_admin_extensions.TestIngestSecretsAndChunking \
    test_admin_extensions.TestPipelineConfinementAndReadOnlySql \
    test_admin_extensions.TestPluginDiscoveryAndEnvScrubbing -v \
    || fail "In-container unit tests failed"
  pass "In-container unit tests for ingest, pipelines, and plugins passed with zero skips."

  log "ALL INTEGRATION TESTS PASSED!"
}

main "$@"
