# Screenshot Workflow

This workflow keeps product screenshots repeatable, safe to publish, and easy to refresh when the UI changes.

## Capture Inputs

Use [screenshot-capture-manifest.json](../screenshot-capture-manifest.json) as the source of truth for screenshot targets. It defines the route, viewport, output filename, wait selector, and notes for each image.

Default output directory:

```text
docs/images/screenshots/
```

Default desktop viewport:

```text
1440 x 1000
```

Default mobile viewport:

```text
390 x 844
```

## Preconditions

Before capturing screenshots, confirm the local control plane is in a stable documentation state:

- The frontend is available at `http://127.0.0.1:8090/`.
- The API is available to the frontend and no page is stuck in a loading state.
- Sign-in state is valid, or local auth bypass is intentionally enabled for documentation.
- Demo Azure resources are selected and any visible data is safe to publish.
- At least one representative BLAST job exists before capturing Jobs or Results pages.
- The browser console has no new errors for the page being captured.
- The page is captured in the intended theme and palette.

## Redaction Rules

Do not publish screenshots that expose secrets or tenant-specific identifiers. Mask or avoid:

- Subscription IDs, tenant IDs, principal IDs, object IDs, and client IDs.
- User emails, UPNs, account names, and organization-only labels.
- SAS URLs, bearer tokens, session tokens, cookies, and API keys.
- Full Storage, ACR, Container App, or Key Vault names when they identify a private environment.
- Command output that includes credentials, private endpoint hostnames, or internal IP addresses.

Prefer stable demo labels and shortened identifiers such as `b0523...` when an identifier is necessary for context.

## Capture Steps

For each entry in the manifest:

1. Open the `path` under the configured `baseUrl`.
2. Set the requested viewport.
3. Wait for `waitForSelector` and then wait for network and animation idle.
4. Check that no sensitive values are visible.
5. Capture the full page or clipped region requested by the manifest.
6. Save the image to `output`.
7. Add or update the matching user guide page with the screenshot and a short explanation.
8. Rebuild the docs with `uv run mkdocs build`.

## Acceptance Checks

A screenshot is ready for publication only when:

- It shows a useful, non-empty state.
- Text is legible at the published image width.
- No menus, tooltips, or loading spinners accidentally cover primary content.
- The image has no private identifiers or secrets.
- The corresponding guide text explains what the reader should look at.
- The MkDocs build succeeds after the image and guide changes.

## Refresh Cadence

Refresh screenshots after user-facing layout changes, navigation changes, or new workflow features. For narrow backend-only changes, update screenshots only when the visible state changes.