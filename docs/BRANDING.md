# ARK Branding & Design System

This document defines the visual identity, logo assets, and color palette used across the ARK (`ark-stack`) web consoles and themes.

## Logo Assets

All UI assets are bundled locally to maintain strict air-gap compliance (no external CDNs or remote fonts):

- **ARK Control Plane & User Portal:** [`admin/static/ark-logo.svg`](../admin/static/ark-logo.svg)
- **ARK LLM Backend Console:** [`llm-admin/static/ark-logo.svg`](../llm-admin/static/ark-logo.svg)
- **Open WebUI Theme:** [`config/open-webui/custom.css`](../config/open-webui/custom.css)

## Color Palette

| Color | Hex | Usage |
| :--- | :--- | :--- |
| **Deep Void** | `#0A0E17` | Midnight background & boundaries |
| **Steel Shield** | `#1E293B` | System & container panels |
| **Signal Amber** | `#F55A00` | Admin alerts, isolation badges & offline status banner |
| **Tech Cyan** | `#00E5FF` | RAG actions, data retrieval & AI features |
| **Glint White** | `#E2E8F0` | High-contrast UI text |
| **Armor Gray** | `#64748B` | Infrastructure & secondary labels |

## Shared UI Layer & Accessibility

The Control Plane, User Portal, Project Console and Create Studio share [`admin/static/ark-ui.css`](../admin/static/ark-ui.css) and [`admin/static/ark-ui.js`](../admin/static/ark-ui.js) (local files only). They provide the focus ring, confirm dialog, toast stack, ARIA tab keyboard support, busy-button handling, overflow menus and inline validation (`arkConfirm`/`confirmModal`, `arkToast`/`showToast`, `arkSwitchTab`, `arkMenu`, `arkFieldError`, plus the shared `el` DOM builder). Do not re-implement these per page.

- **Armor Gray (`#64748B`)** is for decorative or secondary elements. For helper text and table headers on Steel Shield panels use the lighter `--ark-text-soft` (`#94A3B8`) for readable contrast.
- Destructive actions must go through the confirm dialog, never the native `confirm()`.
