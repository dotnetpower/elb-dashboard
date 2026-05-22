---
title: Browser Terminal (Agent Detail)
description: How the browser terminal sidecar works — loopback ttyd, the WebSocket proxy in the api sidecar, the exec-server contract, persistence, and the toolchain shipped in terminal/Dockerfile.
tags:
  - agent
  - terminal
---

# Browser Terminal — Sidecar Lifecycle (detail)

> Extracted from `.github/copilot-instructions.md` §6 on 2026-05-19.

The Browser Terminal is the `terminal` sidecar in the `ca-elb-dashboard` Container App. It carries the `elastic-blast` toolchain and is reached from the SPA via xterm.js → WebSocket → loopback `ttyd`. **There is no Remote Terminal VM, no SSH, no admin password, no NSG, no public IP.** The previous Function-App + Remote-Terminal-VM model has been deleted from the repository.

## Image (`terminal/Dockerfile`)

The `terminal` image is built by `az acr build` during `postprovision.sh`. It must:

* Be Ubuntu-based and install `azure-cli` ≥ 2.81, `kubectl` ≥ 1.34, `azcopy` ≥ 10.28, Python 3.12 + `python3.12-venv`, `git`, `make`, `jq`, `unzip`, `curl`, `tmux`, and `ttyd`.
* Clone `https://github.com/dotnetpower/elastic-blast-azure.git` into `/opt/elastic-blast-azure` at build time and `pip install` its `requirements/test.txt` into a venv that is on PATH for the operator.
* Default `ENTRYPOINT` runs `ttyd` bound to **127.0.0.1** only (the `api` sidecar is the only client; never expose `ttyd` on the public ingress).
* Set `~/.bashrc` to export `PYTHONPATH=src:$PYTHONPATH`, `AZCOPY_AUTO_LOGIN_TYPE=AZCLI`, `ELB_SKIP_DB_VERIFY=true`, `ELB_DISABLE_AUTO_SHUTDOWN=1`, and write a MOTD telling the user the next step is `az login --use-device-code`.

## Persistence

`/home/azureuser` is mounted from the `terminal-home` Azure Files share. That keeps the `~/.azure/` profile, kubeconfig, ssh known_hosts, and any staged query files across revisions and restarts.

## Browser path

* The SPA page (e.g. `BrowserTerminal`) opens a WebSocket to `/api/terminal/ws` on the `api` sidecar.
* The `api` sidecar validates the bearer token + role, then proxies the WebSocket to `127.0.0.1:7681` inside the `terminal` sidecar.
* No download, no SSH client, no password reveal. Display "Run `az login --use-device-code` first" as a one-time helper banner.

## Lifecycle controls

There is no "Destroy Remote Terminal" action because there is no VM. The lifecycle controls reduce to:

* **Restart terminal** — restart the `terminal` sidecar process (`ttyd`) without rolling the revision.
* **Reset home** — clear `/home/azureuser` on the Files share (must require explicit confirmation; this drops the cached `az login`).
