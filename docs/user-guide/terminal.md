---
title: Browser Terminal (User Guide)
description: Operator shell that runs inside the ElasticBLAST Control Plane Container App with the same network and identity boundary — no laptop install required.
tags:
  - user-guide
  - terminal
---

# Browser Terminal

The Browser Terminal puts a shell inside the same network and identity boundary as the control plane, so operators never need to install Azure tools on a laptop. It is reached from the sidebar **Terminal** entry or any `/terminal` link.

## When To Use It

Use the browser terminal when you genuinely need a shell — for example:

- Running `elastic-blast` or `kubectl` commands the dashboard does not expose yet.
- Inspecting AKS pod logs or `kubectl describe` output for a failed job.
- Driving `azcopy` between the workspace Storage account and a curated dataset.
- Running `az` queries after an explicit browser-terminal sign-in.

For everything researchers do day-to-day — submitting a search, watching a job, downloading results — keep using the Dashboard, [New Search](new-search.md), [BLAST Jobs](jobs.md), and [Results](results.md). The dashboard wraps the same operations safely (no token paste, no command typos, no destructive defaults).

## Overview

The static documentation preview does not emulate terminal access: this page
requires an authenticated one-shot WebSocket ticket and a live loopback ttyd
process. Use the deployed control plane (or the full local sidecar stack) to
inspect the current terminal UI.

The page has two regions:

- **Terminal pane** (centre) — an [xterm.js](https://xtermjs.org/) view connected through a WebSocket proxy to the `terminal` sidecar's loopback [ttyd](https://github.com/tsl0922/ttyd). Standard keyboard shortcuts work; output scrollback is kept for 10,000 lines.
- **Side panel** (right) — a **Cockpit** view by default, with a **Manual** view available from the panel toggle. The cockpit surfaces curated workflows, command snippets, terminal health, and Azure CLI sign-in status; the manual pulls full how-tos for `elastic-blast`, `kubectl`, and `azcopy`. Snippet rows let you **Copy** to the clipboard or **Insert** directly into the live terminal.

Status pills near the prompt show the connection state — `connecting`, `connected`, `disconnected`, or `error` — and the caller display name plus the shell user the session is running as.

## How The Connection Works

The browser cannot send `Authorization` headers on a WebSocket upgrade, so the page uses a two-step exchange:

1. `POST /api/terminal/ticket` — the api sidecar validates your MSAL bearer and returns a short-lived single-use ticket.
2. `WebSocket /api/terminal/ws?ticket=<ticket>` — the api proxies bytes to the `terminal` sidecar's loopback ttyd (`127.0.0.1:7681`).

The `terminal` sidecar ships with the validated ElasticBLAST toolchain (`elastic-blast`, `kubectl`, `azcopy`, `az`, `BLAST+`). `$HOME` (`/home/azureuser`) is **ephemeral** — user files and personal Azure CLI login state do not survive a Container App revision swap, so stage data to workload Storage via `azcopy`. The terminal is never exposed directly to the internet — only the api sidecar can reach it.

## Azure Identity For Commands

The api/worker programmatic exec channel signs in with the shared
user-assigned managed identity at sidecar startup. Each browser operator gets a
separate tmux session and Azure CLI cache, so one person's token is never reused
by another person's shell.

Run this on the first interactive use of a session:

```bash
az login --use-device-code
```

The browser tab keeps focus while you complete the device-code flow. Verify the
interactive identity with `az account show`; this login exists only inside your
per-operator shell and is lost on revision restart.

The cockpit's Azure CLI status endpoint probes the programmatic exec cache, not
your private interactive cache. It can therefore show the shared MI as signed
in while your shell still needs device-code login. Changing the interactive
identity does not change the identity used by api, worker, or exec-server
commands.

## Safety Rules

The terminal is a real shell — treat it like one:

- Read commands before you press Enter, especially ones the cockpit marks with a destructive impact badge.
- Never paste tokens, subscription IDs, or full SAS URLs into a shared screen.
- Prefer the cockpit's curated snippets over typing flag combinations from memory.
- Use `kubectl --context …` deliberately; the session inherits whatever `kubeconfig` the sidecar shipped with.
- Close the tab when you are done. Tickets expire automatically, but tidy disconnects are still better.

## Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| Connection stays on `connecting` | Ticket exchange timed out (default 8 s). | Refresh the page; if it persists, check the api sidecar health from the Dashboard. |
| `Terminal unavailable` page | The terminal bundle failed to load (route is feature-flagged). | Click **Reload Terminal**; if the feature flag is off in your deployment, the link will stay hidden. |
| `error` status with `403` | MSAL bearer rejected. | Sign back in to the dashboard tab and reopen `/terminal`. |
| `az` commands say `Please run 'az login'` | Your per-operator interactive cache is new or was cleared by a revision restart. | Run `az login --use-device-code`, then verify with `az account show`. The cockpit may still show the separate exec-channel MI as healthy. |
| Commands run but Storage is unreachable | Production Storage has `publicNetworkAccess: Disabled` — the sidecar reaches it via the platform private endpoint. | Run the command from inside the deployed Container App, not from a laptop tunnelling to a dev cluster. |

## Screenshot Targets

Screenshots for this page are defined by this manifest target:

- `terminal-desktop`

Before capture: connect a fresh session, clear the scrollback, and run something harmless like `elastic-blast --version` or `az account show --query name`. Mask subscription IDs, UPNs, account names, and tokens. Do not publish a screenshot of an active `az login` device-code prompt.
