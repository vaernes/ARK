## Summary

<!-- Describe what this PR changes and why. -->

## Related Issue

<!-- Fixes #(issue) -->

## Air-Gap & Security Checklist

- [ ] No external CDNs, remote fonts, remote scripts/stylesheets, telemetry, or runtime internet downloads introduced.
- [ ] Offline/telemetry-disable flags preserved across `docker-compose.yml` and configs.
- [ ] Caddy/OAuth2-Proxy/`X-Ark-Proxy-Secret` auth boundaries and `/v1/*` block preserved.
- [ ] Multi-tenant project isolation and `admin/audit.py` audit logging preserved.
- [ ] No `innerHTML` usage with dynamic data in `admin/static/*.html`.

## Verification

- [ ] `bash -n scripts/*.sh`
- [ ] `python3 -m unittest discover -s tests -v`
- [ ] `./scripts/test-integration.sh`
- [ ] `CHANGELOG.md` updated (if user-facing)
