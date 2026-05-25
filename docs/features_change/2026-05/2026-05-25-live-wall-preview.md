# Live Wall preview opt-in

## Motivation

Live Wall is an experimental monitor surface. It should stay hidden unless a browser user explicitly opts into preview features from Settings.

## User-facing change

- Added a disabled-by-default `Live Wall` toggle under Settings > Preview.
- The main navigation shows `Live Wall` only when that preview preference is enabled.
- Direct navigation to `/monitor/live-wall` redirects to Dashboard while the preview preference is disabled.
- Settings > Preview hides the footer `Reset` and `Done` buttons so preview toggles read as immediately applied preferences.

## API/IaC diff summary

- No API changes.
- No IaC changes.
- Frontend preferences now include `previewLiveWallEnabled` in the existing `elb-prefs` localStorage payload.

## Validation evidence

- `npm --prefix web run build` passed.
- Browser check on `http://127.0.0.1:8090/`: with `previewLiveWallEnabled=false`, `/monitor/live-wall` redirected to `/` and the navigation omitted `Live Wall`.
- Browser check on `http://127.0.0.1:8090/monitor/live-wall`: with `previewLiveWallEnabled=true`, the navigation showed `Live Wall` and the page rendered heading `Live Wall`.
- Headless Playwright check on `http://127.0.0.1:8090/`: Settings > Preview showed the `Live Wall preview` toggle and omitted `Reset` / `Done`; Settings > Appearance still showed both footer actions.