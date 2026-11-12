# Security Policy

## Supported Versions

ARK uses Calendar Versioning (`YYYY.MM.PATCH`). Security fixes are applied to the
current release branch and tagged as patch releases.

| Version | Supported |
| :--- | :--- |
| `2026.11.x` | Yes |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security issue in ARK (such as an air-gap leak, authentication
or proxy-header bypass, cross-tenant RAG data exposure, or backup/restore path
traversal), please report it privately:

1. Open a private **Security Advisory** on GitHub:
   `https://github.com/vaernes/ARK/security/advisories/new`
2. Include as much detail as possible:
   - Affected ARK version (`VERSION` or `/admin-console/api/version`)
   - Affected component (`Caddyfile`, `ark-admin`, `oauth2-proxy`, `scripts/`, etc.)
   - Reproduction steps or proof-of-concept
   - Potential impact on air-gapped isolation, RBAC, or tenant boundaries

We aim to acknowledge reports within **3 business days** and provide an initial
remediation plan or advisory timeline within **7 business days**.

## Security Architecture Highlights

ARK is built for strictly air-gapped enterprise environments and enforces:

- **Zero External Network Calls:** No remote CDNs, fonts, scripts, update checks,
  or telemetry endpoints.
- **Fail-Closed Proxy Boundary:** `ark-admin` rejects `/admin-console/*` and
  `/portal/*` requests lacking a valid `X-Ark-Proxy-Secret` injected by Caddy
  after `oauth2-proxy` authentication.
- **Blocked Raw Inference Gateway:** Direct `/v1/*` routes return HTTP `403` at
  Caddy so clients must authenticate through `/projects/<slug>/v1/*` with
  scoped `ark_proj_*` keys.
- **Artifact & Backup Integrity:** Offline bundles (`ark-images.tar.zst`,
  `ark-models.tar.zst`, `ark-bundle.json`) and backup archives (`manifest.json`)
  require SHA-256 verification before loading or restoring.
