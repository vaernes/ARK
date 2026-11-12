# Contributing to ARK (`ark-stack`)

Thank you for contributing to **ARK (Air-gapped Retrieval & Knowledge)**!

## Non-Negotiable Engineering Rules

Before writing code, read [AGENTS.md](AGENTS.md). Every contribution must preserve:

1. **Strict Air-Gap Compliance:** Never introduce external CDNs, remote fonts,
   remote scripts/stylesheets, telemetry, or runtime internet downloads.
2. **Auth & Gateway Boundaries:** Keep `/v1/*` blocked (`403`) at Caddy, keep
   `/admin-console/*` gated by `oauth2-proxy` (`ark-admins`) + `X-Ark-Proxy-Secret`,
   and keep `/projects/<slug>/v1/*` scoped to `ark_proj_*` keys.
3. **Multi-Tenant Isolation:** Never allow one project's API key or user group to
   access another project's Qdrant collection or endpoint.
4. **Auditability:** Record admin mutations and project gateway requests through
   `admin/audit.py`.
5. **Safe UI Rendering:** Never use `innerHTML` with dynamic data in
   `admin/static/index.html` or `admin/static/portal.html` (use DOM element
   helpers and `textContent`).

## Local Development & Verification (No GPU Required)

You can develop and test ARK on any Linux workstation with Docker installed:

```bash
# 1. Run shell syntax & Python unit tests
bash -n scripts/*.sh
python3 -m unittest discover -s tests -v

# 2. Run the full GPU-less Docker integration suite
./scripts/test-integration.sh

# 3. Optional: spin up an interactive local dev environment at https://localhost:8443
./scripts/dev-up.sh
./scripts/dev-up.sh --down
```

## Versioning & Release Process

ARK uses **CalVer (`YYYY.MM.PATCH`)** stored in [`VERSION`](VERSION).

- Feature/bugfix PRs should update [`CHANGELOG.md`](CHANGELOG.md) under `## [Unreleased]`.
- Maintainers bump versions with:
  ```bash
  ./scripts/bump-version.sh YYYY.MM.PATCH
  ```
- Pushing a matching tag (`git tag -a vYYYY.MM.PATCH -m "Release vYYYY.MM.PATCH"`)
  triggers `.github/workflows/release.yml`.

## Pull Request Checklist

- [ ] Changes comply with all air-gap and zero-telemetry invariants.
- [ ] `bash -n scripts/*.sh` and `python3 -m unittest discover -s tests -v` pass.
- [ ] `./scripts/test-integration.sh` passes after final edits.
- [ ] `CHANGELOG.md` and docs (`README.md`, `docs/`) are updated if user-facing behavior changed.

## License

By contributing to ARK, you agree that your contributions will be licensed under
the **Apache License, Version 2.0** (see [LICENSE](LICENSE)).
