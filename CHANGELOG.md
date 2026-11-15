# Changelog

All notable changes to **ARK (`ark-stack`)** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Calendar Versioning (CalVer)](https://calver.org/)
using `YYYY.MM.PATCH` (for example, `2026.11.0`).

## [Unreleased]

### Changed
- **UI/UX pass across Admin Console, User Portal, Project Console and Create Studio (no visual rebrand):**
  - Added shared local assets `admin/static/ark-ui.css` and `admin/static/ark-ui.js` (served from the existing `/admin-console/static`, `/portal/static` and `/create/static` mounts; no external resources) providing a visible `:focus-visible` ring, an accessible confirm dialog (focus move/trap/restore, `Esc` and backdrop close), keyboard-operable ARIA tabs (arrow keys, `aria-controls`) and KPI cards, busy-state protection against double submits, inline field validation, higher-contrast helper text, a sticky error banner, and small-screen/reduced-motion rules.
  - User Portal: revoking an API key now asks for confirmation, a newly issued key shows a dedicated "shown once" box with a **Copy** button and toast, and API errors show the server's `detail` message.
  - Replaced the native `confirm()` for model-profile deletion and added a confirmation for deleting a Create Studio creation.
  - Unified header navigation order and labels across pages (Open WebUI Chat, User Portal, Create Studio, Admin Console, Sign Out) with the current page highlighted.
  - Removed the duplicated toast, confirm-dialog, tab-switching and DOM-builder code from the Admin Console and Project Console in favour of the shared `ark-ui.js` helpers (`showToast`, `confirmModal`, `arkSwitchTab`, `el`), and added them to the User Portal and Create Studio.
  - Split the Admin Console "Security, Backups & Audit" tab into **Security & Audit** and **Backups & Recovery** (new `#backups` deep link and badge showing the number of backup bundles).
  - Dense table action cells (projects, model profiles) now keep the primary actions visible and move the rest, including destructive ones, into an accessible **More** overflow menu (`arkMenu`).
  - Fixed horizontal overflow of the User Portal on narrow screens and refreshed the Control Plane, User Portal and Create Studio screenshots in `docs/images/`.
  - CI air-gap check now also scans `create.html`, `project.html`, `ark-ui.css` and `ark-ui.js` for external URLs.

### Added
- **ARK Create Studio (`/create/`) & Image Generation Support:**
  - Added **ARK Create Studio** (`/create/`) protected by Keycloak SSO (`oauth2-proxy` `forward_auth` + `X-Ark-Proxy-Secret`), featuring visual style presets (`photorealistic`, `illustration`, `isometric_diagram`, `icon`, `slide_graphic`, `blueprint`, `none`), aspect ratio/size controls (`1024x1024`, `1344x768`, `768x1344`), AI prompt enhancement via the ARK LLM (`POST /create/api/enhance-prompt`), and a per-user PostgreSQL-backed creation gallery (`/create/api/creations`).
  - Added per-project OpenAI-compatible image generation gateway endpoint (`POST /projects/<slug>/v1/images/generations`) gated by the `images` API key scope, per-key rate limits, and structured audit logging (`api.image_generation`, `create.generate`, `create.enhance_prompt`, `create.delete`, `image_backend.update`).
  - Added runtime Image Generation Backend configuration in the Admin Console (`/admin-console/api/image-backend`), Open WebUI image generation wiring in `docker-compose.yml` and `scripts/dev-up.sh`, and deterministic stdlib PNG mock inference in `tests/mock_inference.py`.

## [2026.11.1] - 2026-11-15

### Added
- **Project-to-Project RAG Linking & Per-Project Snapshots:**
  - Added explicit read-only RAG linking across projects via `extra_rag_projects`, `PATCH /admin-console/api/projects/{slug}/rag-projects`, Admin Console controls, User Portal indicators, and `--extra-rag-project` in `provisioning/bootstrap_ark.py`.
  - Added per-project Qdrant snapshot export, download, and upload routes (`/admin-console/api/projects/{slug}/snapshot/*`) and CLI helpers (`scripts/export-project-kb.sh` with `--out` support and `scripts/sync-project-kb.sh`).
- **Security, RBAC & Ingestion Platform:**
  - Added Fernet encryption at rest for connector/LLM backend secrets (`ARK_SECRETS_KEY`), fail-closed `X-Ark-Proxy-Secret` header validation, per-user project RBAC (`/portal/`, `/admin-console/api/projects/{slug}/members`), and scoped project API keys (`chat` / `ingest`).
  - Added declarative `ark-ingest/v1` pipelines, env-scrubbed plugin and custom Python ingest execution with path/SQL confinement, 5-field cron scheduling, and `audit_logs.tar.gz` checksum verification (`extra_archives`) in backups.
  - Added integration assertions in `scripts/test-integration.sh` and unit coverage in `tests/test_admin_extensions.py`.

### Changed
- Auto-generated project RAG system prompts synchronize automatically when linked RAG projects are updated or removed.
- Data Ingestion target project selector (`#conn-proj`) filters to RAG-enabled projects only, and Clean projects disable linked RAG inputs in the Admin Console.
- CI and release workflows install `admin/requirements.txt` before running unit tests so ingest, pipeline, and plugin tests execute with zero skips.

### Removed
- Removed the hardcoded global `Company_Common` collection, automatic cross-project provisioning/querying, and legacy `scripts/*-company-kb.sh` scripts in favor of explicit project-to-project RAG linking and `scripts/*-project-kb.sh`.

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

[Unreleased]: https://github.com/vaernes/ARK/compare/v2026.11.1...HEAD
[2026.11.1]: https://github.com/vaernes/ARK/compare/v2026.11.0...v2026.11.1
[2026.11.0]: https://github.com/vaernes/ARK/releases/tag/v2026.11.0
