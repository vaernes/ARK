# ARK Installation & Setup Guide

This guide walks through bundling ARK at a connected HQ environment, installing it on an air-gapped Linux GPU server, and configuring corporate SSO, TLS, backups, and SIEM forwarding.

---

## 1. Prerequisites

### HQ Build Machine (Internet-Connected)
- Linux x86_64 host with Docker Engine, `zstd`, `sha256sum`, and `huggingface_hub[cli]` (`hf` or `huggingface-cli`).
- At least **100 GB** free disk space for container images and Hugging Face model weights.

### Air-Gapped Office Server
- **OS:** Linux (Ubuntu 22.04/24.04 or RHEL 8/9 recommended).
- **GPU:** NVIDIA GPU(s) with recent NVIDIA drivers and `nvidia-container-toolkit` installed (`nvidia-smi` must succeed).
- **Container Runtime:** Docker Engine + Docker Compose v2 (`docker compose`).
- **Utilities:** `zstd`, `curl`, `sha256sum`, Python 3.10+.
- **Network / DNS:** An internal DNS A-record (e.g., `ark.corp.internal`) pointing to the server's LAN IP.

---

## 2. Phase 1 — Bundle Artifacts at Connected HQ

1. Clone or copy the `ark-stack` repository on the connected build machine.
2. Create a `.env` file from the template if you want to customize image tags or model IDs:
   ```bash
   cp .env.example .env
   ```
3. Run the offline bundler:
   ```bash
   ./scripts/bundle-offline.sh --package-models
   ```
   This will:
   - Pull all required container images (`vllm`, `tei`, `qdrant`, `open-webui`, `postgres`, `tika`, `caddy`, `keycloak`, `oauth2-proxy`).
   - Build `ark-admin:latest` from `./admin`.
   - Save and compress all images into `./artifacts/ark-images.tar.zst` (+ `.sha256`).
   - Download `Qwen/Qwen2.5-14B-Instruct`, `BAAI/bge-m3`, and `BAAI/bge-reranker-v2-m3` into `./artifacts/models/` and package `./artifacts/ark-models.tar.zst` (+ `.sha256`).
4. Transfer the repository and `./artifacts/` directory to the air-gapped server via approved offline media.

---

## 3. Phase 2 — Configure Secrets on the Air-Gapped Server

On the air-gapped server, create `.env` from `.env.example` and replace all placeholder secrets:

```bash
cp .env.example .env
```

Generate strong random values for:
- `VLLM_API_KEY`
- `QDRANT_API_KEY`
- `POSTGRES_PASSWORD`
- `WEBUI_SECRET_KEY`
- `KEYCLOAK_ADMIN_PASSWORD`
- `OIDC_OPENWEBUI_CLIENT_SECRET`
- `OIDC_OAUTH2_PROXY_CLIENT_SECRET`
- `OAUTH2_PROXY_COOKIE_SECRET` (must be 32-byte URL-safe base64, e.g. `python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'`)

Optional enterprise settings in `.env`:
- **SIEM Forwarding:** Set `SIEM_SYSLOG_HOST`, `SIEM_SYSLOG_PORT`, and `SIEM_SYSLOG_PROTOCOL` (`udp` or `tcp`).

---

## 4. Phase 3 — Run the Offline Installer

Run the installer script:
```bash
./scripts/install-offline.sh
```

During interactive execution, the installer will prompt you for:
1. **Internal FQDN** (sets `ARK_DOMAIN` and `WEBUI_URL` in `.env`, and updates `config/keycloak/ark-realm.json`).
2. **Backup Destination Path** (sets `ARK_BACKUP_DIR` in `.env`; this can also be changed later in the `ark-admin` UI).

You can also pass these non-interactively:
```bash
./scripts/install-offline.sh \
  --domain ark.corp.internal \
  --backup-dir /mnt/nas/ark-backups \
  --non-interactive
```

The installer verifies `nvidia-smi`, free disk space, and `.sha256` checksums, loads Docker images (`zstd -d -c | docker load`), unpacks model weights into `./artifacts/models/`, creates `./data/*` directories, and runs `docker compose up -d`.

Verify all containers are healthy:
```bash
docker compose ps
```

---

## 5. Phase 4 — TLS Certificates

- **Default (Caddy Internal CA):** Caddy automatically provisions an internal CA and serves HTTPS. Export the root certificate and distribute it to office workstations (or via GPO):
  ```bash
  docker cp ark-caddy:/data/caddy/pki/authorities/local/root.crt ./ark-root-ca.crt
  ```
- **Corporate PKI / ADCS (Recommended for Domain-Joined PCs):**
  1. Place your internal CA-signed certificate and private key at `./certs/ark.crt` and `./certs/ark.key`.
  2. In [config/caddy/Caddyfile](../config/caddy/Caddyfile), replace `tls internal` with:
     ```caddyfile
     tls /certs/ark.crt /certs/ark.key
     ```
  3. Restart Caddy: `docker compose restart caddy`.

---

## 6. Phase 5 — Configure Keycloak SSO & Active Directory

Keycloak boots with the `ark` realm pre-imported from [config/keycloak/ark-realm.json](../config/keycloak/ark-realm.json).

1. Open `https://<your-ark-domain>/auth/admin/` and log in with `KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD`.
2. Select the **`ark`** realm → **User Federation** → **Add Ldap providers** (for Active Directory / LDAPS) or configure **Identity Providers** (for SAML 2.0 / ADFS) or **Kerberos**.
   - If using LDAPS, place your AD Root CA certificate at `./certs/ad-ca.crt` (mounted at `/certs/ad-ca.crt`).
   - Add a **Group Mapper** in Keycloak User Federation so AD security groups sync into Keycloak groups.
3. Assign IT administrators to the **`ark-admins`** group in Keycloak (required to access `/admin-console/`).

---

## 7. Phase 6 — Initial Knowledge Sync & Project Setup

1. **Sync `Company_Common` from HQ:**
   ```bash
   ./scripts/sync-company-kb.sh --snapshot /path/to/Company_Common.snapshot
   ```
2. **Generate an Open WebUI Admin API Key** (optional, for syncing Workspace Assistants into Open WebUI):
   - Sign in to `https://<your-ark-domain>` as an admin, go to **Settings → Account → API Keys**, and add `OPENWEBUI_API_KEY=...` to `.env`, then restart `ark-admin` (`docker compose up -d ark-admin`).
3. **Create your first project** via the Admin UI (`https://<your-ark-domain>/admin-console/`) or via CLI (`python3 provisioning/bootstrap_ark.py --create-project "Project_Alpha"`).

For day-to-day usage after installation, see the **[User & Operations Guide](USER_GUIDE.md)**.
