---
name: ark-admin-feature
description: >-
  Step-by-step pattern for adding or modifying admin endpoints, document
  connectors, or UI controls in ark-admin while preserving air-gap, RBAC,
  multi-tenant isolation, and audit logging.
---

# Adding Features to `ark-admin`

Follow this checklist when adding a new API endpoint, document connector, or control-plane capability:

1. **Database Schema (`config/postgres/init.sql`)**
   - If new persistent state is needed in `ark_admin`, update `config/postgres/init.sql` (and any startup migrations in `admin/app.py` if existing DBs must upgrade cleanly).
2. **Connectors (`admin/connectors.py`)**
   - Keep ingestion offline-safe (Apache Tika + TEI embeddings + Qdrant).
   - Scope ingested chunks strictly to the target project's Qdrant collection; never write into another project's collection or `Company_Common` unless explicitly intended.
3. **API & Gateway (`admin/app.py`)**
   - Admin endpoints under `/admin-console/` rely on `oauth2-proxy` headers (`X-Forwarded-User`, `X-Forwarded-Email`) injected by Caddy.
   - Project API endpoints under `/projects/<slug>/v1/` must validate the `ark_proj_*` key against the target `<slug>` and enforce rate limits.
4. **Audit Logging (`admin/audit.py`)**
   - Log every admin mutation and project API request via `admin/audit.py` so events land in PostgreSQL `ark_admin.audit_logs`, `./data/audit/audit.jsonl`, and Syslog SIEM (if configured).
5. **Admin UI (`admin/static/index.html`)**
   - Use pure local HTML/CSS/JS—no external CDNs, fonts, or third-party scripts.
   - Match the ARK color palette (`#0A0E17`, `#1E293B`, `#F55A00`, `#00E5FF`, `#E2E8F0`, `#64748B`).
6. **Verify**
   - Run `./scripts/test-integration.sh` to confirm the `ark-admin` image builds and all integration checks pass.
