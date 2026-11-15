# ARK Installation & Setup Guide

This guide walks through bundling ARK at a connected HQ environment, installing it on an air-gapped Linux GPU server, and configuring corporate SSO, TLS, backups, and SIEM forwarding.

---

## 1. Prerequisites

### HQ Build Machine (Internet-Connected)
- Linux x86_64 host with Docker Engine + BuildKit (`docker-buildx`), `zstd`, `sha256sum`, and `huggingface_hub[cli]` (`hf` or `huggingface-cli`).
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
3. Generate an Ed25519 signing key at HQ, store the private key securely outside
   the repository, and transfer its public key separately through a trusted
   channel:
   ```bash
   umask 077
   openssl genpkey -algorithm ED25519 -out /secure/ark-bundle.key
   openssl pkey -in /secure/ark-bundle.key -pubout -out /secure/ark-bundle.pub
   ```
4. Run the offline bundler:
   ```bash
   ./scripts/bundle-offline.sh --package-models --signing-key /secure/ark-bundle.key
   ```
   This will:
   - Pull all required container images (`vllm`, `tei`, `qdrant`, `open-webui`, `postgres`, `tika`, `caddy`, `keycloak`, `oauth2-proxy`).
   - Build `ark-admin:latest` from `./admin`.
   - Save and compress all images into `./artifacts/ark-images.tar.zst` (+ `.sha256`).
   - Download `Qwen/Qwen2.5-14B-Instruct`, `BAAI/bge-m3`, and `BAAI/bge-reranker-v2-m3` into `./artifacts/models/` and package `./artifacts/ark-models.tar.zst` (+ `.sha256`).
5. Transfer the repository and `./artifacts/` directory to the air-gapped server via approved offline media.
   The signed manifest binds image and model archives to their SHA-256 hashes.
   Use a separately authenticated public key; a key copied inside the same
   untrusted bundle cannot establish provenance.

---

## 3. Phase 2 — Configure Secrets on the Air-Gapped Server

On the air-gapped server, create `.env` from `.env.example` and replace all placeholder secrets:

```bash
cp .env.example .env
```

The installer and snapshot tools read `.env` as data, not shell commands.
Use single quotes around literal `$` characters in values (for example,
`POSTGRES_PASSWORD='a$long-secret'`); shell substitutions and multiline values
are not supported. Existing values containing spaces or LDAP filters do not
need shell escaping.

Generate strong random values for:
- `VLLM_API_KEY`
- `QDRANT_API_KEY`
- `POSTGRES_PASSWORD`
- `WEBUI_SECRET_KEY`
- `KEYCLOAK_ADMIN_PASSWORD`
- `OIDC_OPENWEBUI_CLIENT_SECRET`
- `OIDC_OAUTH2_PROXY_CLIENT_SECRET`
- `OAUTH2_PROXY_COOKIE_SECRET` (must be 32-byte URL-safe base64, e.g. `python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'`)
- `ARK_PROXY_AUTH_SECRET` (shared secret between Caddy and `ark-admin` to block peer-container proxy bypass, e.g. `python3 -c 'import secrets; print(secrets.token_hex(32))'`)
- `LLM_ADMIN_TOKEN` (a separate token for the LLM Backend Console; the offline
  installer generates one when it is unset)

Optional enterprise settings in `.env`:
- **SIEM Forwarding:** Set `SIEM_SYSLOG_HOST`, `SIEM_SYSLOG_PORT`, and `SIEM_SYSLOG_PROTOCOL` (`udp` or `tcp`).
- **Connected mode:** Set `ARK_CONNECTED_MODE=true` only when intentionally
  using a public cloud provider. The default blocks the built-in public LLM
  provider hosts, but it is not a general egress firewall: enforce the
  organization's approved destination allowlist at the host/network boundary,
  including connectors, DNS, image services, and alternate hostnames.

The LLM Backend Console binds to localhost by default and rejects management
requests when its token is unset. Existing standalone deployments that relied
on the inference key must configure `LLM_ADMIN_TOKEN` explicitly. For remote
administration, expose it only through a trusted encrypted channel.

---

## 4. Phase 3 — Run the Offline Installer

Run the installer script:
```bash
./scripts/install-offline.sh --trusted-key /secure/ark-bundle.pub
```

During interactive execution, the installer will prompt you for:
1. **Internal FQDN** (sets `ARK_DOMAIN` and `WEBUI_URL` in `.env`, and updates `config/keycloak/ark-realm.json`).
2. **Backup Destination Path** (sets `ARK_BACKUP_DIR` in `.env`; this can also be changed later in the `ark-admin` UI).

You can also pass these non-interactively:
```bash
./scripts/install-offline.sh \
  --domain ark.corp.internal \
  --backup-dir /mnt/nas/ark-backups \
  --trusted-key /secure/ark-bundle.pub \
  --non-interactive
```

The installer verifies the Ed25519 signature, archive hashes, `nvidia-smi`,
and free disk space before loading images and models. For legacy or
development bundles only, `--allow-unsigned` explicitly disables signature
authentication. Signed installs require the signed model archive; `--models-src`
is supported only with `--allow-unsigned`.

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

## 7. Phase 6 — Initial Project Setup & Optional Snapshot Import

1. **Generate an Open WebUI Admin API Key** (optional, for syncing Workspace Assistants into Open WebUI):
   - Sign in to `https://<your-ark-domain>` as an admin, go to **Settings → Account → API Keys**, and add `OPENWEBUI_API_KEY=...` to `.env`, then restart `ark-admin` (`docker compose up -d ark-admin`).
2. **Create your projects** via the Admin UI (`https://<your-ark-domain>/admin-console/`) or via CLI (`python3 provisioning/bootstrap_ark.py --create-project "Shared_KB"` and `--create-project "Project_Alpha" --extra-rag-project shared-kb` if you want `Project_Alpha` to search `Shared_KB` read-only).
3. **Optionally sync a prebuilt project Qdrant snapshot** (e.g., exported from HQ):
   ```bash
   ./scripts/sync-project-kb.sh --collection Shared_KB --snapshot /path/to/Shared_KB.snapshot
   ```

---

## 8. Phase 7 — Backup Verification & Disaster Recovery

For a complete, consistent appliance backup, use the cold-backup tool below.
The Admin Console's online backup remains limited to PostgreSQL and Qdrant:
its database dumps and snapshots are sequential and do not provide a
cross-service point-in-time guarantee.

### Complete encrypted cold backup

Create two distinct RSA recovery key pairs and one Ed25519 signing key. Keep
the data and secret private keys in separate protected offline escrow; keep
the signing private key on the appliance with owner-only permissions, and
distribute the signing public key through a separately trusted channel:

```bash
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out /secure/data.key
openssl pkey -in /secure/data.key -pubout -out /secure/data.pub
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out /secure/secrets.key
openssl pkey -in /secure/secrets.key -pubout -out /secure/secrets.pub
openssl genpkey -algorithm ED25519 -out /secure/backup-signing.key
openssl pkey -in /secure/backup-signing.key -pubout -out /secure/backup-signing.pub
```

Run a backup during an approved maintenance window:

```bash
./scripts/backup-appliance.sh --backup /mnt/secure-backups/ark-cold-001 \
  --data-key /secure/data.pub --secrets-key /secure/secrets.pub \
  --signing-key /secure/backup-signing.key
```

The tool stops the Compose services, saves their current images, archives model
weights and persistent PostgreSQL/Qdrant/Open WebUI/audit/plugin/script data,
and separately encrypts site configuration, `.env`, certificates, and Caddy
CA state. Both archives use authenticated encryption and the manifest is
signed. Previously running services are restarted even if backup fails.
Provide enough space for the image archive and encrypted backup; set `TMPDIR`
to a suitably protected filesystem if `/tmp` is too small.

On a clean host with a trusted `ark-admin` image already loaded, verify or
restore into a **new**, nonexistent directory:

```bash
./scripts/restore-appliance.sh --backup /mnt/secure-backups/ark-cold-001 \
  --data-key /secure/data.key --secrets-key /secure/secrets.key \
  --verify-key /secure/backup-signing.pub --verify-only --work-dir /mnt/secure-work
./scripts/restore-appliance.sh --backup /mnt/secure-backups/ark-cold-001 \
  --data-key /secure/data.key --secrets-key /secure/secrets.key \
  --verify-key /secure/backup-signing.pub --target /srv/ark-restored
zstd -d -c /srv/ark-restored/artifacts/ark-recovery-images.tar.zst | docker load
cd /srv/ark-restored
docker compose --profile local-llm up -d
```

Decryption uses temporary plaintext on the recovery filesystem; use encrypted
storage and remove any abandoned temporary directory after an interrupted
recovery. Restore private keys and the signing verification key independently
of the backup. Losing `ARK_SECRETS_KEY` or the secret recovery key prevents
recovery of encrypted connector credentials.

Backups created from the Admin Console (`Back Up Databases & Vectors`) write a timestamped directory under `ARK_BACKUP_DIR` (`ark-backup-<UTC_TIMESTAMP>/`) containing:
- `openwebui.sql.gz`, `keycloak.sql.gz`, and `ark_admin.sql.gz` (PostgreSQL dumps)
- `qdrant/<collection>/<snapshot>.snapshot` (downloaded Qdrant collection snapshots)
- `manifest.json` (SHA-256 checksums and file inventory for all databases and Qdrant snapshots)

### 1. Verify Backup Integrity Before Transfer or Restore
```bash
./scripts/restore-backup.sh --backup-dir /path/to/ark-backup-YYYYMMDDTHHMMSSZ --verify-only
```
This validates every file in `manifest.json` against its SHA-256 digest and verifies `gzip` stream integrity on all PostgreSQL archives.

### 2. Database and Vector Restore via CLI
Ensure `postgres` and `qdrant` containers are healthy (`docker compose up -d postgres qdrant`), then run:
```bash
./scripts/restore-backup.sh --backup-dir /path/to/ark-backup-YYYYMMDDTHHMMSSZ
```
If you are restoring on a clean host, restart the application services afterward so cached state reloads cleanly:
```bash
docker compose up -d
```

### 3. Restore via Admin Console
For online database and vector rollback while `ark-admin` is running, select a
verified bundle in **Backups & Recovery → Restore Databases & Vectors** and
click **Verify & Restore Backup** (calls `POST /admin-console/api/backups/restore`).

### 4. Post-Restore Verification
1. Check container health: `docker compose ps`
2. Verify PostgreSQL row counts:
   ```bash
   docker exec ark-postgres psql -U ark_webui -d ark_admin -c "SELECT count(*) FROM projects;"
   ```
3. Verify Qdrant collections report `"status":"ok"` and expected point counts:
   ```bash
   curl -sf -H "api-key: ${QDRANT_API_KEY}" http://127.0.0.1:6333/collections
   ```
4. Sign in to `https://<your-ark-domain>/` and run a test query against a restored project assistant.

For day-to-day usage after installation, see the **[User & Operations Guide](USER_GUIDE.md)**.
