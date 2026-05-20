# Browser Terminal

The Browser Terminal provides an in-browser shell for operator workflows that require ElasticBLAST or Azure command-line tools inside the control-plane environment.

## What To Explain

- When to use the terminal and when to stay in the dashboard.
- The connection path through the API sidecar.
- Safe command-output handling.
- How to avoid publishing secrets in screenshots.

## Screenshot Targets

Screenshots for this page are defined by this manifest target:

- `terminal-desktop`

Clear or avoid sensitive scrollback before capture.# Terminal

The Terminal page provides browser access to the terminal sidecar for controlled ElasticBLAST operations.

## Screenshot Slot

Capture target: `docs/images/screenshots/terminal-session.png`

Recommended state before capture:

- The terminal is connected.
- The prompt is visible after a harmless command such as `elastic-blast --version` or `az account show --query name`.
- Subscription IDs, account IDs, access tokens, and user names are masked.

## Notes To Cover

- When to use the browser terminal.
- How the terminal sidecar differs from a local shell.
- Which commands are safe to show in public documentation.