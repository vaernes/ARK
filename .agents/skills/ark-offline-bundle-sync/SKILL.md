---
name: ark-offline-bundle-sync
description: >-
  Checklist for updating container images, Hugging Face model IDs, or
  environment variables across the ARK offline bundling and installation
  pipeline. Use when bumping versions, adding services, or changing models.
---

# ARK Offline Bundle & Config Sync Checklist

ARK splits deployment into an online HQ bundling phase (`scripts/bundle-offline.sh`) and an air-gapped office installation phase (`scripts/install-offline.sh`). Keep these files synchronized whenever images, models, or env vars change:

1. **Environment Template (`.env.example`)**
   - Add or update image tags, model IDs, ports, or feature flags with safe defaults.
2. **Compose Definitions (`docker-compose.yml` & `docker-compose.test.yml`)**
   - Ensure any new service sets offline/no-telemetry environment variables (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `DO_NOT_TRACK=1`, etc. as applicable) and mounts local volumes only.
   - Update `docker-compose.test.yml` if the new service needs a GPU-less mock or override.
3. **HQ Bundler (`scripts/bundle-offline.sh`)**
   - Ensure any new Docker image is pulled, saved, and zstd-compressed into `artifacts/`.
   - Ensure any new Hugging Face model is downloaded into `artifacts/models/` and included in `sha256sums`.
4. **Offline Installer (`scripts/install-offline.sh`)**
   - Verify checksum validation, `docker load`, and volume/directory initialization match the artifacts produced by `bundle-offline.sh`.
5. **Documentation (`docs/INSTALL.md` & `README.md`)**
   - Update hardware/disk requirements, env var tables, or verification steps if user-facing setup changed.
