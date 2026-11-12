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
   - **RAG Project Assistant** (e.g., `Project Alpha Assistant`): Searches both the shared `Company_Common` knowledge base and your project's isolated documents (`Project_Alpha`), and includes source citations.
   - **Clean Project Assistant** (e.g., `Clean Sandbox Assistant`): Isolated chat without document retrieval—useful for general drafting, coding, or brainstorming.
2. **Keep projects separated:** Start a new chat when switching between projects so conversation context never mixes.
3. **Uploading project files:** In your project's Workspace/Knowledge tab (if granted write access), upload PDFs, Word, Excel, PowerPoint, or text files. ARK extracts text offline via Apache Tika and indexes it into that project's isolated Qdrant collection only.

### 3. Connecting Office Add-ins, IDEs & Internal Tools
If your administrator issued a **Project API Key** (`ark_proj_...`) for your team, configure your OpenAI-compatible tool (Office add-in, VS Code Continue/Cline, or Python script) with:

- **Base URL:** `https://<your-ark-domain>/projects/<project-slug>/v1`
- **API Key:** `ark_proj_...`
- **Model:** `<project-slug>-assistant` (or any value—the gateway routes to the project's configured model and RAG context)

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
> **Note:** Direct unauthenticated calls to `/v1/chat/completions` are blocked (`403 Forbidden`). All API traffic must go through `/projects/<slug>/v1/` or Open WebUI so RBAC and audit logging are enforced.

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
  - **RAG-Enabled:** Creates isolated Qdrant collections (`Company_Common` + `Project_Alpha`), Open WebUI Knowledge Bases, an RBAC group (`project-alpha`), and `Project Alpha Assistant`.
  - **Clean Project (No RAG):** Creates an isolated project and `Clean Sandbox Assistant` backed directly by `vLLM` without any document retrieval.
- **System Prompt (optional):** Override the default assistant instructions.

### 3. Issuing & Revoking Project API Keys
In the **Project API Keys** panel:
1. Select the target project, enter a descriptive label (e.g., `Word Add-in - Finance`), and set a rate limit (requests per minute).
2. Click **Issue API Key** and copy the `ark_proj_...` key immediately (it is only displayed once; only its SHA-256 hash is stored).
3. Click **Revoke** next to any key to immediately block access.

### 4. Connecting Document Sources (M-Files, SharePoint On-Prem, SMB/NFS)
In the **Document Connectors** panel, select a **RAG-enabled** project, choose the connector type, and paste the JSON configuration:

- **Mounted SMB / NFS Folder (`smb_folder`):**
  ```json
  {
    "path": "/mnt/shares/project_alpha"
  }
  ```
- **SharePoint On-Premises (`sharepoint`):**
  ```json
  {
    "site_url": "https://intranet.corp.internal/sites/engineering",
    "folder_server_relative_url": "/sites/engineering/Shared Documents/ProjectAlpha",
    "username": "CORP\\svc-ark-sp",
    "password": "service-account-password",
    "verify_tls": true
  }
  ```
- **M-Files (`mfiles`):**
  ```json
  {
    "base_url": "https://mfiles.corp.internal",
    "vault_guid": "{11111111-2222-3333-4444-555555555555}",
    "username": "svc-ark-mfiles",
    "password": "service-account-password",
    "object_type": 0,
    "verify_tls": true
  }
  ```

Click **Add Connector**, then click **Sync** to extract documents via Apache Tika, embed with `BAAI/bge-m3`, and upsert into the project's Qdrant collection.

### 5. Updating `Company_Common` from HQ
1. Obtain the latest `Company_Common.snapshot` (and `.sha256`) exported from HQ.
2. Either:
   - **Admin UI:** Upload `Company_Common.snapshot` in the **Backup & Company_Common Sync** panel, **or**
   - **CLI:** Run `./scripts/sync-company-kb.sh --snapshot /path/to/Company_Common.snapshot`.

### 6. Backups & Changing the Backup Directory
In the **Backup & Company_Common Sync** panel:
- **Change Backup Location:** Update **Backup Destination Directory** (e.g., `/backups/nas`) and click **Save Backup Path**.
- **Run Backup:** Click **Run Full Backup Now** to create compressed `pg_dump` archives (`openwebui.sql.gz`, `keycloak.sql.gz`, `ark_admin.sql.gz`) and Qdrant snapshots for every collection.

### 7. Monitoring Health & Audit Logs
- **Service Health:** Displays live status for `vllm`, `tei_embed`, `tei_rerank`, `qdrant`, `postgres`, `tika`, `open_webui`, and `keycloak`.
- **Audit Logs:** Displays recent project creation, API key issuance/revocation, API chat requests, denied/rate-limited attempts, connector syncs, and backups. All events are also written to `./data/audit/audit.jsonl` and forwarded via Syslog if `SIEM_SYSLOG_HOST` is set in `.env`.
