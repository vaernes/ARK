# Changelog

All notable changes to **ARK (`ark-stack`)** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Calendar Versioning (CalVer)](https://calver.org/)
using `YYYY.MM.PATCH` (for example, `2026.11.0`).

## [Unreleased]

## [2026.11.0] - 2026-11-13

### Added
- **Project Versioning & Licensing:**
  - Adopted **Apache License 2.0** (`LICENSE`, `NOTICE`) and **CalVer** (`VERSION`).
  - Added `/admin-console/api/version` and `/portal/api/version` endpoints, OCI image labels (`org.opencontainers.image.version`, `org.opencontainers.image.licenses`), version badges/footers in the Admin Console and User Portal, and `ark_version` tracking in backup `manifest.json` and offline bundle metadata (`ark-bundle.json`).
  - Added `--version` (`-V`) support to `scripts/bundle-offline.sh`, `scripts/install-offline.sh`, `scripts/update-llm.sh`, and `provisioning/bootstrap_ark.py`, plus `scripts/bump-version.sh`.
  - Added governance documentation (`CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`) and GitHub issue/PR templates and tag-based release workflow (`.github/workflows/release.yml`).
- **Core Air-Gapped Stack:**
  - Multi-container Infrastructure-as-Code stack (`docker-compose.yml`) for **vLLM**, **TEI Embeddings/Reranker**, **Qdrant**, **Apache Tika**, **PostgreSQL 16**, **Keycloak**, **OAuth2-Proxy**, **Open WebUI**, **Caddy**, and **ark-admin**.
  - Strict air-gap and zero-telemetry enforcement (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `DO_NOT_TRACK=1`, `OFFLINE_MODE=true`, `QDRANT__TELEMETRY_DISABLED=true`, `VLLM_NO_USAGE_STATS=1`, Caddy `ocsp_stapling off` / `tls internal`).
- **Security, SSO & Multi-Tenant Isolation:**
  - Fail-closed `X-Ark-Proxy-Secret` + `oauth2-proxy` `forward_auth` boundary protecting `/admin-console/*` (`ark-admins`) and `/portal/*` (authenticated users), with raw `/v1/*` blocked (`403`).
  - Multi-tenant RAG (`Company_Common` + isolated project Qdrant collections + RBAC groups) and Clean non-RAG project isolation.
  - Scoped per-project OpenAI-compatible API gateway (`/projects/<slug>/v1/chat/completions`, including SSE streaming) with `ark_proj_*` keys and per-key RPM rate limiting.
  - Self-service User API Key Portal (`/portal/`).
- **Operations, Connectors & Testing:**
  - Offline document connectors for **SharePoint On-Prem**, **M-Files**, and **SMB/NFS** folders.
  - LLM/embedding/reranker management UI, API, and atomic host updater (`scripts/update-llm.sh`) with rollback.
  - Full-stack PostgreSQL + Qdrant SHA-256-verified backup and disaster-recovery restore (`admin/backup_utils.py`, `scripts/restore-backup.sh`).
  - Structured audit logging to PostgreSQL (`ark_admin.audit_logs`), `./data/audit/audit.jsonl`, and optional Syslog SIEM.
  - GPU-less integration test suite (`scripts/test-integration.sh`), local dev stack (`scripts/dev-up.sh`), and Python unit tests (`tests/test_admin_unit.py`).

[Unreleased]: https://github.com/vaernes/ARK/compare/v2026.11.0...HEAD
[2026.11.0]: https://github.com/vaernes/ARK/releases/tag/v2026.11.0
