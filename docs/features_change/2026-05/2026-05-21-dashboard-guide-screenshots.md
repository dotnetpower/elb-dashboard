# Dashboard guide screenshots

## Motivation

The Dashboard user guide had placeholder instructions for future screenshots and duplicated page content. It needed real captured images so the public MkDocs guide can demonstrate the current Dashboard layout.

## User-Facing Change

- Replaced the duplicated Dashboard guide stub with a single guide page.
- Added desktop and mobile Dashboard screenshots captured from the local frontend.
- Linked both screenshots directly from the Dashboard user guide.

## API/IaC Diff Summary

- No API or infrastructure changes.
- Documentation-only update under `docs/user-guide/` and `docs/images/screenshots/`.

## Validation Evidence

- Captured `docs/images/screenshots/dashboard-overview-desktop.png` from `http://127.0.0.1:8090/` with a 1440 px desktop viewport.
- Captured `docs/images/screenshots/dashboard-mobile.png` from `http://127.0.0.1:8090/` with a 390 px mobile viewport.
- `file docs/images/screenshots/dashboard-overview-desktop.png docs/images/screenshots/dashboard-mobile.png` confirmed both PNG files.