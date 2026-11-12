---
name: ark-integration-test
description: >-
  Run, debug, and extend the GPU-less ARK end-to-end integration test suite
  (scripts/test-integration.sh and tests/mock_inference.py). Use when validating
  changes to Caddy, Keycloak, oauth2-proxy, ark-admin, Postgres, or Qdrant, or
  when adding new integration assertions.
---

# ARK Integration Test Workflow

Use this skill when verifying or extending the GPU-less Docker integration suite.

## 1. Running the Suite

From the repository root:

```bash
./scripts/test-integration.sh
```

This builds `ark-admin:latest` from `admin/`, creates an isolated Docker network (`ark-test-net`), and starts:
- `ark-test-postgres` (`config/postgres/init.sql`)
- `ark-test-qdrant` (`config/qdrant/config.yaml`)
- `ark-test-vllm` & `ark-test-tei-embed` (`tests/mock_inference.py`)
- `ark-test-keycloak` (`config/keycloak/ark-realm.json` rewritten for `https://localhost:8443`)
- `ark-test-oauth2-proxy`
- `ark-test-ark-admin`
- `ark-test-caddy` (`config/caddy/Caddyfile`)

## 2. Debugging Failures

Run with `--keep` to leave containers running after the run:

```bash
./scripts/test-integration.sh --keep
```

Useful inspection commands while `--keep` is active:
- `docker ps --filter name=ark-test-`
- `docker logs ark-test-ark-admin`
- `docker logs ark-test-caddy`
- `docker logs ark-test-keycloak`

Clean up manually when done:

```bash
docker rm -f $(docker ps -aq --filter name=ark-test-)
docker network rm ark-test-net
```

## 3. Extending the Suite

When adding a new feature (e.g., a new `ark-admin` route, Caddy rule, or connector check):
1. If the feature calls vLLM or TEI, ensure `tests/mock_inference.py` handles the endpoint cleanly without network access.
2. Add assertions to `scripts/test-integration.sh` using `pass` / `fail` helpers, testing both the authorized path and unauthorized/cross-tenant rejection.
3. Re-run `./scripts/test-integration.sh` end-to-end and verify exit code `0`.
