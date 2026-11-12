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
TMP_ROOT="$(mktemp -d /tmp/ark-test-XXXXXX)"
PG_PASS="ark-test-pg-pass"
KC_PASS="ark-test-kc-pass"
QDRANT_KEY="ark-test-qdrant-key"
VLLM_KEY="ark-test-vllm-key"
COOKIE_SECRET="$(python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"

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
  docker network rm "${NET_NAME}" >/dev/null 2>&1 || true
  rm -rf "${TMP_ROOT}" 2>/dev/null || true
}
trap cleanup EXIT

main() {
  mkdir -p "${TMP_ROOT}/backups" "${TMP_ROOT}/audit" "${TMP_ROOT}/keycloak"
  cp "${REPO_ROOT}/config/keycloak/ark-realm.json" "${TMP_ROOT}/keycloak/ark-realm.json"
  sed -i "s|https://ark\.local|https://localhost:8443|g" "${TMP_ROOT}/keycloak/ark-realm.json"

  log "Building ark-admin image..."
  docker build -q -t ark-admin:latest "${REPO_ROOT}/admin" >/dev/null

  log "Creating isolated Docker network '${NET_NAME}'..."
  docker network create "${NET_NAME}" >/dev/null

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
    -e VLLM_URL="http://vllm:8000" \
    -e VLLM_API_KEY="${VLLM_KEY}" \
    -e LLM_SERVED_NAME="ark-llm" \
    -e ARK_MODELS_DIR="/models" \
    -e TEI_EMBED_URL="http://tei-embed:80" \
    -e TEI_RERANK_URL="http://tei-rerank:80" \
    -e TIKA_URL="http://tika:9998" \
    -e OPENWEBUI_INTERNAL_URL="http://open-webui:8080" \
    -e KEYCLOAK_INTERNAL_URL="http://keycloak:8080/auth" \
    -e ARK_BACKUP_DIR="/backups" \
    -e ARK_AUDIT_LOG_FILE="/var/log/ark/audit.jsonl" \
    -v "${TMP_ROOT}/backups:/backups" \
    -v "${TMP_ROOT}/audit:/var/log/ark" \
    -v "${TMP_ROOT}/models:/models" \
    ark-admin:latest >/dev/null

  log "Starting Caddy reverse proxy on 127.0.0.1:8443..."
  docker run -d --name ark-test-caddy --network "${NET_NAME}" --network-alias caddy \
    -p 127.0.0.1:8443:443 \
    -e ARK_DOMAIN="localhost" \
    -v "${REPO_ROOT}/config/caddy/Caddyfile:/etc/caddy/Caddyfile:ro" \
    caddy:2-alpine >/dev/null

  sleep 3

  # 1. Test Keycloak OIDC Discovery through Caddy
  log "1/7 Testing Keycloak OIDC discovery via Caddy..."
  curl -ksf "https://localhost:8443/auth/realms/ark/.well-known/openid-configuration" | grep -q '"issuer"' || \
    fail "Keycloak OIDC discovery failed through Caddy."
  pass "Keycloak OIDC discovery reachable via Caddy."

  # 2. Test Admin Console SSO protection (must redirect to /oauth2/start)
  log "2/7 Testing /admin-console/ SSO forward_auth enforcement..."
  local admin_code
  admin_code=$(curl -ks -o /dev/null -w "%{http_code}" "https://localhost:8443/admin-console/")
  [[ "${admin_code}" == "302" ]] || fail "Expected 302 redirect for unauthenticated /admin-console/, got ${admin_code}"
  pass "Unauthenticated /admin-console/ redirected to SSO (HTTP 302)."

  # 3. Test raw /v1 bypass blocking
  log "3/7 Testing raw /v1/* bypass block..."
  local raw_code
  raw_code=$(curl -ks -o /dev/null -w "%{http_code}" "https://localhost:8443/v1/chat/completions")
  [[ "${raw_code}" == "403" ]] || fail "Expected 403 for raw /v1/*, got ${raw_code}"
  pass "Raw /v1/* bypass blocked (HTTP 403)."

  # 4. Create RAG project + Clean project via ark-admin
  log "4/7 Provisioning RAG project (Project_Alpha) and Clean project (Clean_Sandbox)..."
  docker exec ark-test-ark-admin python3 -c "
import requests
r1 = requests.post('http://localhost:8090/admin-console/api/projects', json={'name': 'Project_Alpha', 'rag_enabled': True})
r1.raise_for_status()
r2 = requests.post('http://localhost:8090/admin-console/api/projects', json={'name': 'Clean_Sandbox', 'rag_enabled': False})
r2.raise_for_status()
"
  pass "Created Project_Alpha (RAG) and Clean_Sandbox (Clean)."

  # 5. Issue API keys & ingest a sample document vector into Project_Alpha
  log "5/7 Issuing scoped API keys and testing Qdrant RAG + Clean project routing..."
  local keys_json
  keys_json=$(docker exec ark-test-ark-admin python3 -c "
import json, requests
from qdrant_client import QdrantClient
from qdrant_client.http.models import PointStruct
from connectors import embed_texts

k1 = requests.post('http://localhost:8090/admin-console/api/keys', json={'project_slug': 'project-alpha', 'label': 'office-addin'}).json()['api_key']
k2 = requests.post('http://localhost:8090/admin-console/api/keys', json={'project_slug': 'clean-sandbox', 'label': 'ide-plugin'}).json()['api_key']

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
keys = requests.get('http://localhost:8090/admin-console/api/keys').json()
clean_id = next(k['id'] for k in keys if k['project_slug'] == 'clean-sandbox')
requests.delete(f'http://localhost:8090/admin-console/api/keys/{clean_id}').raise_for_status()
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
requests.delete('http://localhost:8090/admin-console/api/projects/clean-sandbox').raise_for_status()
"
  pass "Deleted Clean_Sandbox project via API."

  # 6. Test LLM & Model Management APIs
  log "6/8 Testing LLM & Model Management APIs..."
  docker exec ark-test-ark-admin python3 -c "
import requests
m = requests.get('http://localhost:8090/admin-console/api/models', timeout=10)
m.raise_for_status()
info = m.json()
assert info['vllm_live']['reachable'] is True, info
assert 'ark-llm' in info['vllm_live']['models'], info
dirs = {x['dir'] for x in info['local_models']}
assert 'Qwen--Qwen2.5-14B-Instruct' in dirs and 'Qwen--Qwen2.5-32B-Instruct' in dirs, dirs

r_def = requests.post('http://localhost:8090/admin-console/api/models/default', json={'model_name': 'ark-llm-v2'})
r_def.raise_for_status()
assert r_def.json()['default_llm_model'] == 'ark-llm-v2'
requests.post('http://localhost:8090/admin-console/api/models/default', json={'model_name': 'ark-llm'}).raise_for_status()

r_proj = requests.patch('http://localhost:8090/admin-console/api/projects/project-alpha/model', json={'llm_model': 'ark-llm-custom'})
r_proj.raise_for_status()
assert r_proj.json()['effective_llm_model'] == 'ark-llm-custom'
requests.patch('http://localhost:8090/admin-console/api/projects/project-alpha/model', json={'llm_model': ''}).raise_for_status()

r_plan = requests.post('http://localhost:8090/admin-console/api/models/switch-plan', json={'model_dir': 'Qwen--Qwen2.5-32B-Instruct', 'model_type': 'llm'})
r_plan.raise_for_status()
assert './scripts/update-llm.sh --type llm --model-dir Qwen--Qwen2.5-32B-Instruct' in r_plan.json()['command']
"
  pass "LLM & Model Management endpoints verified."

  # 7. Test Backup Runner
  log "7/8 Testing full PostgreSQL + Qdrant backup..."
  docker exec ark-test-ark-admin python3 -c "
import requests
r = requests.post('http://localhost:8090/admin-console/api/backups/run', timeout=60)
r.raise_for_status()
data = r.json()
assert len(data['databases']) == 3, data
assert len(data['qdrant_snapshots']) >= 2, data
"
  pass "Backup created 3 pg_dump archives and Qdrant snapshots."

  # 8. Test Audit Logs
  log "8/8 Verifying audit logs..."
  docker exec ark-test-ark-admin python3 -c "
import requests
logs = requests.get('http://localhost:8090/admin-console/api/audit?limit=30').json()
events = {l['event_type'] for l in logs}
assert 'project.create' in events
assert 'project.delete' in events
assert 'project.model_update' in events
assert 'model.default_update' in events
assert 'model.switch_plan' in events
assert 'apikey.create' in events
assert 'apikey.revoke' in events
assert 'api.chat_completion' in events
assert 'api.auth_failed' in events
assert 'backup.run' in events
"
  pass "All expected events recorded in PostgreSQL audit_logs."

  log "ALL INTEGRATION TESTS PASSED!"
}

main "$@"
