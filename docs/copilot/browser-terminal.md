---
title: Browser Terminal (Agent Detail)
description: How the browser terminal sidecar works — loopback ttyd, the WebSocket proxy in the api sidecar, the exec-server contract, persistence, and the toolchain shipped in terminal/Dockerfile.
tags:
  - agent
  - terminal
---

# Browser Terminal — Sidecar Lifecycle (detail)

> Re-verified 2026-09-16 against `terminal/entrypoint.sh`, `exec_server.py`, and
> the terminal WebSocket routes.

The Browser Terminal is the `terminal` sidecar in the `ca-elb-dashboard` Container App. It carries the `elastic-blast` toolchain and is reached from the SPA via xterm.js → WebSocket → loopback `ttyd`. **There is no Remote Terminal VM, no SSH, no admin password, no NSG, no public IP.** The previous Function-App + Remote-Terminal-VM model has been deleted from the repository.

## Image (`terminal/Dockerfile`)

The `terminal` image is built by `az acr build` during `postprovision.sh`. It must:

* Be Ubuntu-based and install `azure-cli` ≥ 2.81, `kubectl` ≥ 1.34, `azcopy` ≥ 10.28, Python 3.12 + `python3.12-venv`, `git`, `make`, `jq`, `unzip`, `curl`, `tmux`, and `ttyd`.
* Clone the pinned `dotnetpower/elastic-blast-azure` commit into `/opt/elb/elastic-blast-azure`, apply the dashboard runtime patch, install `requirements/base.txt`, and install the package into `/opt/elb/venv`.
* Default `ENTRYPOINT` supervises loopback `ttyd` on `:7681`, the authenticated exec server on `:7682`, and a non-critical cgroup metrics reporter. The `api` sidecar is the only ttyd/exec client; neither port is public ingress.
* Set the operator profile and MOTD, isolate each browser operator into a stable tmux session plus its own `AZURE_CONFIG_DIR`, and bootstrap the separate programmatic exec cache with the shared UAMI.

## Persistence

`/home/azureuser` is **ephemeral**. The earlier design mounted a `terminal-home` Azure Files share, but SMB mounts in Container Apps require a Storage account key, which conflicts with the platform Storage account's `allowSharedKeyAccess: false` invariant. The control plane is designed to tolerate ephemeral terminal state: user query/result files stage to workload Storage via `azcopy`, the exec server's shared-UAMI cache is recreated at sidecar startup, and each operator repeats device-code login after a revision swap.

## Browser path

* The SPA page (e.g. `BrowserTerminal`) opens a WebSocket to `/api/terminal/ws` on the `api` sidecar.
* The `api` sidecar validates `require_caller` when issuing a 30-second,
  one-shot ticket, validates Origin at redemption, then proxies the WebSocket
  to `127.0.0.1:7681`. There is no separate terminal app-role gate today.
* No download, no SSH client, no password reveal. The cockpit Azure probe uses the programmatic exec channel, so it reports that cache's MI status rather than a browser operator's private tmux cache. The operator should run `az account show` in the shell and use `az login --use-device-code` when interactive credentials are needed.

## Lifecycle

There is no "Destroy Remote Terminal" or in-app single-sidecar restart action
because there is no VM and all containers share one Container App replica.
Host-mode development may run `docker compose restart terminal`; in Azure, a
revision/replica restart affects the bundled sidecars. The terminal page treats
WebSocket loss as recoverable and obtains a fresh one-shot ticket when the
sidecar returns.

A revision or terminal-process restart discards `/home/azureuser`, rebuilds the
managed-identity CLI caches, and preserves durable files only when they were
staged to workload Storage.
