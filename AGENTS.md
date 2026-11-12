# ARK (`ark-stack`) Agent Instructions

ARK (Air-gapped Retrieval & Knowledge) is a Linux-based Infrastructure-as-Code appliance for local LLM inference, enterprise SSO, and multi-tenant RAG in **strictly air-gapped (offline)** environments.

## Non-Negotiable Invariants

1. **Strict Air-Gap & Zero External Network Calls**
   - Never add external CDNs, remote fonts, remote scripts/stylesheets, or external API calls to `admin/static/index.html`, `admin/*.py`, `provisioning/*.py`, or container definitions.
   - Preserve offline and telemetry-disable flags across `docker-compose.yml` and configs: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `DO_NOT_TRACK=1`, `OFFLINE_MODE=true`, `QDRANT__TELEMETRY_DISABLED=true`, `VLLM_NO_USAGE_STATS=1`, and Caddy `ocsp_stapling off` / `tls internal`.
   - Any new Python package added to `admin/requirements.txt` or `provisioning/requirements.txt` must work offline at runtime without downloading weights, schemas, or assets from the internet.

2. **Routing, Auth & API Gateway Boundaries (`config/caddy/Caddyfile`)**
   - Keep raw `/v1/*` blocked (`403`) so clients cannot bypass `ark-admin` key validation, rate limits, and audit logging.
   - Keep `/admin-console*` gated behind `oauth2-proxy` (`forward_auth`) and Keycloak (`ark-admins` group).
   - Keep `/projects/<slug>/v1/*` routed to `ark-admin:8090` with `ark_proj_*` key validation.

3. **Multi-Tenant Compartmentalization**
   - Preserve isolation between RAG-enabled projects (dedicated Qdrant collection + RBAC group) and clean non-RAG projects.
   - Never allow a project-scoped `ark_proj_*` key from one project to query or access another project's collection or endpoint.

4. **Auditability**
   - Admin mutations and `/projects/<slug>/v1/*` API calls must record structured audit events via `admin/audit.py` (PostgreSQL `ark_admin.audit_logs`, `./data/audit/audit.jsonl`, and optional Syslog SIEM).

5. **UI & Branding (`admin/static/index.html`)**
   - Use local assets only (e.g., `admin/static/ark-logo.svg`).
   - Follow the ARK palette: Deep Void `#0A0E17`, Steel Shield `#1E293B`, Signal Amber `#F55A00`, Tech Cyan `#00E5FF`, Glint White `#E2E8F0`, Armor Gray `#64748B`.

## Verification

- Before finishing changes to `admin/`, `config/`, `docker-compose*.yml`, or `scripts/`, run the GPU-less integration test suite:
  ```bash
  ./scripts/test-integration.sh
  ```
- Use `./scripts/test-integration.sh --keep` if you need to inspect containers on `ark-test-net` (`docker rm -f $(docker ps -aq --filter name=ark-test-)` to clean up).
