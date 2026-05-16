---
agent: "agent"
description: "Full frontend QA review for elb-dashboard — browser walkthrough, screenshots, console/network checks, and severity-ranked findings"
---

## Instructions

1. Open the shared browser page if one is available; otherwise open `http://localhost:8090`.
2. If the app is not reachable, start the workspace `fullstack: start` task and retry. Do not ask the user to run local commands.
3. If authentication is required, use the current dev bypass flow when available. If the Setup Wizard appears, complete only non-destructive local configuration from existing subscriptions/resource groups/storage/ACR choices. Do not create Azure resources, enqueue provisioning tasks, build images, submit BLAST jobs, reset terminal home, or run destructive actions unless the user explicitly asked for that.
4. Review the current Container Apps sidecar architecture. The Terminal page is the browser terminal sidecar (`/api/terminal/ws` to loopback `ttyd`), not a Remote Terminal VM. Treat any VM, SSH, public IP, admin password, or "Destroy VM" UI as a bug.
5. Capture screenshots at 1366×768 for:
   - Dashboard `/` — top and bottom after scrolling.
   - New Search `/blast/submit` — include validation/preview states without submitting a real job.
   - Jobs `/blast/jobs` — and a job detail/analytics route only if an existing job is naturally available from the UI.
   - Custom DB `/blast/databases/build` — include disabled/preview states.
   - Lab Tools `/tools` — verify preview-only or disabled states are honest.
   - Terminal `/terminal` — verify sidecar availability messaging and helper copy.
   - API `/docs`.
6. Repeat a focused responsive pass at a narrow mobile viewport (about 390×844) for Dashboard, navigation, New Search, and Terminal. Screenshot only pages with visible layout problems.
7. Toggle the theme from the top bar and screenshot the Dashboard in the alternate theme. Check text readability, contrast, focus states, and whether status colours remain distinguishable in both themes.
8. Check the browser console and network log for JavaScript errors, React warnings, failed static assets, failed API calls, unexpected 401/403/500 responses, and noisy polling. A documented 503 for an intentionally pending/stub backend is acceptable only if the UI clearly presents it as unavailable or preview-only.
9. Verify data honesty and safety:
   - Dashboard cards must show real API-derived values, empty/error states, and last-refreshed context rather than fabricated healthy data.
   - Storage must never imply browser SAS downloads or public access; `publicNetworkAccess` should be shown as Disabled when present.
   - Destructive or expensive actions must be disabled, guarded by confirmation, or clearly scoped.
   - Loading, empty, degraded, and permission-denied states must be understandable without leaking secrets, subscription IDs, bearer tokens, or SAS URLs.
10. Produce a review with findings first, ordered by severity. Use this structure:
   - **Issues**: table columns `#`, `Severity` (`Critical`, `High`, `Medium`, `Low`, `Minor`), `Area`, `Detail`, `Evidence`.
   - **Strengths**: concise notes on layout, workflow clarity, visual polish, real-time updates, and accessibility.
   - **Open Questions**: only include genuine uncertainties or unavailable data.
   - **Overall Assessment**: one short paragraph.
11. Severity guide:
   - `Critical`: data loss, secret exposure, destructive/expensive action without protection, broken auth gate, or a browser path that cannot load the app.
   - `High`: primary workflow blocked, severe false status/data, sidecar/Storage security model misrepresented, or broad console/network failures.
   - `Medium`: important page or state confusing, inaccessible, visually broken, or missing expected safeguards.
   - `Low`: localised usability, copy, layout, or responsive issue.
   - `Minor`: polish issue with little workflow impact.
