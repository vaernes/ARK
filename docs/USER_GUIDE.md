# ARK User & Operations Guide

This guide covers day-to-day usage of **ARK (Air-gapped Retrieval & Knowledge)** for **End Users** (chat, documents, IDE/Office integrations) and **Administrators** (Control Plane, projects, connectors, backups, and audits).

---

## Part 1: End-User Guide

### 1. Logging In
1. Open your browser to `https://<your-ark-domain>` (for example, `https://ark.corp.internal`).
2. Click **Corporate SSO** and sign in with your normal company username and password (Active Directory / SSO).
3. Your project permissions are applied automatically based on your IT security groups.

### 2. Using Project Chats in Open WebUI
1. In the top-left **Model selector**, choose the Assistant for the project you are working on:
   - **RAG Project Assistant** (e.g., `Project Alpha Assistant`): Searches your project's isolated documents (`Project_Alpha`), plus any other RAG projects explicitly linked to it read-only (e.g., `Shared_KB`), and includes source citations.
   - **Clean Project Assistant** (e.g., `Clean Sandbox Assistant`): Isolated chat without document retrieval—useful for general drafting, coding, or brainstorming.
2. **Keep projects separated:** Start a new chat when switching between projects so conversation context never mixes.
3. **Uploading project files:** In your project's Workspace/Knowledge tab (if granted write access), upload PDFs, Word, Excel, PowerPoint, or text files. ARK extracts text offline via Apache Tika and indexes it into that project's isolated Qdrant collection only.

### 2b. Generating Images & Diagrams in ARK Create Studio (`/create/`)
An administrator must configure an image-capable backend and set
`ENABLE_IMAGE_GENERATION=true`; image generation is disabled by default.
1. Open `https://<your-ark-domain>/create/` (or click **Create Studio** from the User Portal or Admin Console).
2. Enter a description of the image, diagram, icon, or slide graphic you want to create, or click **Enhance Prompt (ARK LLM)** to expand a short idea into a rich visual prompt.
3. Choose a **Visual Style Preset** (`Photorealistic`, `Digital Illustration`, `Isometric Diagram`, `App / UI Icon`, `Slide Graphic`, `Technical Blueprint`, or `Raw Prompt`), select a **Canvas Size / Ratio** (`1024x1024`, `1344x768`, or `768x1344`), and click **Generate Visual Asset**.
4. Download the generated PNG, copy its Data URL, or revisit past assets in **My Recent Creations**.

### 3. Connecting Office Add-ins, IDEs & Internal Tools
If your administrator issued a **Project API Key** (`ark_proj_...`) for your team (or you generated a personal key in `/portal/`), configure your OpenAI-compatible tool (Office add-in, VS Code Continue/Cline, or Python script) with:

- **Base URL:** `https://<your-ark-domain>/projects/<project-slug>/v1`
- **API Key:** `ark_proj_...`
- **Model:** `<project-slug>-assistant` (or any value—the gateway routes to the project's configured model and RAG context)

**Supported OpenAI-compatible & Ingest endpoints on `/projects/<project-slug>/v1`:**
- `GET /models` — returns the project's assistant model metadata (`<project-slug>-assistant`)
- `POST /chat/completions` — supports both non-streaming (`"stream": false`) and Server-Sent Events streaming (`"stream": true`) (requires `chat` key scope)
- `POST /images/generations` — generates images (`b64_json`) via the configured image generation backend with optional `style_preset` support (requires `images` key scope)
- `POST /ingest/documents` — push JSON (`{"documents": [...]}`) or `application/x-ndjson` Document IR records into the project's Qdrant collection (requires `ingest` key scope)
- `POST /ingest/files` — multipart file upload extracted via Apache Tika and indexed into the project's Qdrant collection (requires `ingest` key scope)
- `DELETE /ingest/documents/<doc_id>` — remove a document's vectors from the project's Qdrant collection (requires `ingest` key scope)
- `GET /ingest/documents` & `GET /ingest/jobs/<job_id>` — list tracked document hashes and check async ingest job status (requires `ingest` key scope)
- `POST /ingest/jobs/<job_id>/retry` — retry a failed or partially completed push job within the same project (requires `ingest` key scope)

> **Scope Note:** Other OpenAI endpoints (`/embeddings`, `/completions`, `/audio/*`, `/files/*`, `/assistants/*`) are intentionally not exposed by the project gateway. Direct unauthenticated calls to `/v1/chat/completions` are also blocked (`403 Forbidden`) so RBAC, key scopes (`chat` / `images` / `ingest`), rate limits, and audit logging are always enforced.

**Example `curl` call:**
```bash
curl https://ark.corp.internal/projects/project-alpha/v1/chat/completions \
  -H "Authorization: Bearer ark_proj_..." \
  -H "Content-Type: application/json" \
  -d '{
    "model": "project-alpha-assistant",
    "messages": [{"role": "user", "content": "Summarize the latest safety procedure."}]
  }'
```

---

## Part 2: Administrator Guide

### 1. Accessing the Control Plane (`ark-admin`)
1. Ensure your user account is a member of the **`ark-admins`** group in Keycloak / Active Directory.
2. Open `https://<your-ark-domain>/admin-console/`.
3. You will be authenticated via Corporate SSO (`oauth2-proxy` + Keycloak).

### 2. Creating Projects (RAG vs. Clean)
In the **Create Project** panel:
- **Project Name:** Enter a name such as `Project_Alpha` or `Clean_Sandbox`.
- **Mode:**
  - **RAG-Enabled:** Creates an isolated Qdrant collection (`Project_Alpha`), an Open WebUI Knowledge Base, an RBAC group (`project-alpha`), and `Project Alpha Assistant`. Optionally enter comma-separated RAG project slugs (or click **RAG** in the project table later) to attach another project's knowledge base read-only when needed.
  - **Clean Project (No RAG):** Creates an isolated project and `Clean Sandbox Assistant` backed directly by `vLLM` without any document retrieval.
- **System Prompt (optional):** Override the default assistant instructions.

### 3. Issuing & Revoking Project API Keys
In the **Project API Keys** panel:
1. Select the target project, enter a descriptive label (e.g., `Word Add-in - Finance`), and set a rate limit (requests per minute).
2. Click **Issue API Key** and copy the `ark_proj_...` key immediately (it is only displayed once; only its SHA-256 hash is stored).
3. Click **Revoke** next to any key to immediately block access.

### 4. Flexible Data Ingestion (Plugins, ARK Pipelines, Scripts & Push API)
In the **Data Ingestion (Plugins, Pipelines & Scripts)** panel, select a **RAG-enabled** project and choose one of three source modes (or push directly from external systems via `/projects/<slug>/v1/ingest/*` with an `ingest`-scoped `ark_proj_*` key):

1. **Connector Plugins (`plugin`):**
   - Built-in plugins include `smb_folder`, `sharepoint`, `mfiles`, and `http_json`.
   - Custom offline plugins placed in `./plugins/<plugin-id>/` (with a `manifest.json` declaring `ark_protocol: "1"` and `config_schema`) are discovered automatically and run in an env-scrubbed subprocess streaming NDJSON Document IR.
2. **ARK Declarative Pipelines (`pipeline_yaml`):**
   - Author `ark-ingest/v1` YAML/JSON pipelines directly in the Admin Console (supports `http_json`, `http_paginated`, `sql_query`, `filesystem_glob`, `csv`, `ndjson`) with sandboxed Jinja2 transforms (`html_to_text`, `slugify`, `regex_replace`, `json_get`) and **Dry Run (Preview 5)** before indexing.
3. **Custom Python Scripts (`script`):**
   - Place `.py` scripts using `from ark_ingest import Document, Checkpoint, run` in `./data/ingest-scripts/` (mounted read-only at `/ingest-scripts`). Scripts execute in an env-scrubbed subprocess without access to `ARK_ADMIN_DB_URL` or `QDRANT_API_KEY`.

All modes support **Incremental** (content-hash deduplication + stale chunk cleanup) or **Full Mirror** (prunes deleted documents) sync modes, configurable chunking (`chars`, `markdown`, `lines`, `none`), and schedules (`@15m`, `@hourly`, `@daily`).
Only one sync per source can be queued or running at a time; a manual sync
returns a conflict while an earlier job is active.
Queued jobs and encrypted push payloads survive a service restart. Interrupted
jobs are retried up to `ARK_INGEST_MAX_ATTEMPTS`; failed or partially completed
push jobs can be retried via the project API. Completed push payloads are
deleted. Queue capacity and payload size are configurable; full queues return
`503` and oversized push requests return `413`.

### 5. Exporting & Importing Project KB Snapshots
1. To export a project's Qdrant snapshot for transfer (e.g., HQ to an office deployment):
   - **Admin UI:** In **Backup & Project KB Snapshot Sync**, select the RAG project, click **Export Project Snapshot**, then click **Download .snapshot**.
   - **CLI:** Run `./scripts/export-project-kb.sh --collection Shared_KB --out ./Shared_KB.snapshot`.
2. To import a `.snapshot` (and optional `.sha256`) into a RAG project's collection:
   - **Admin UI:** Select the target RAG project in **Backup & Project KB Snapshot Sync**, choose the `.snapshot` file, and click **Upload & Recover Snapshot**.
   - **CLI:** Run `./scripts/sync-project-kb.sh --collection Shared_KB --snapshot /path/to/Shared_KB.snapshot`.

### 6. Backups, Restore & Changing the Backup Directory
In the **Backup & Project KB Snapshot Sync** panel:
- **Change Backup Location:** Update **Backup Destination Directory** (e.g., `/backups/nas`) and click **Save Backup Path**.
- **Run Backup:** Click **Back Up Databases & Vectors** to create a timestamped bundle containing PostgreSQL dumps, Qdrant snapshots, and a SHA-256 `manifest.json`.
- **Verify & Restore:** Select a bundle under **Restore Databases & Vectors** and click **Verify & Restore Backup** (or use `./scripts/restore-backup.sh` on the host). Application files and separately escrowed secrets require independent recovery; see [docs/INSTALL.md](INSTALL.md#8-phase-7--backup-verification--disaster-recovery).

### 7. Monitoring Health & Audit Logs
- **Service Health:** Displays live status for `vllm`, `tei_embed`, `tei_rerank`, `qdrant`, `postgres`, `tika`, `open_webui`, and `keycloak`.
- **Audit Logs:** Displays recent project creation, API key issuance/revocation, API chat requests, denied/rate-limited attempts, connector syncs, and backups. All events are also written to `./data/audit/audit.jsonl` and forwarded via Syslog if `SIEM_SYSLOG_HOST` is set in `.env`.
