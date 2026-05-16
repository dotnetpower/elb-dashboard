# 2026-05-16 — Allow `sudo apt install/update` in the browser terminal

## Motivation

The browser terminal sidecar runs as non-root `azureuser` with **no `sudo`
binary in the image**, so the operator could not install missing tools
(`htop`, `tcpdump`, etc.) without rebuilding the image. The blanket
`sudo` block in [terminal/command_guard.sh](../../../terminal/command_guard.sh)
made the situation worse by surfacing a misleading "sudo is not
available" message even if `sudo` were ever installed.

The guard's purpose is to stop *destructive* operations (recursive
deletes, `az ... delete`, cluster-wide `kubectl delete`, …); blanket-
blocking package installation overshoots that goal.

## User-facing change

Inside the browser terminal:

- `sudo apt update` / `sudo apt install <pkg> …` (and the `apt-get`
  aliases) **work without a password prompt**.
- `sudo apt remove`, `sudo apt purge`, `sudo apt-get autoremove`,
  `sudo apt-get dist-upgrade`, `sudo dpkg …`, `sudo bash`, `sudo rm …`
  and any other `sudo X` invocation are **still blocked** with a clear
  message: `sudo is restricted to 'apt update' and 'apt install' in the
  browser terminal`.

Installs are still ephemeral — a sidecar restart drops the image
back to the baked-in toolchain. (Persistent overlay was considered and
rejected: it conflicts with the read-only-image / Azure Files-for-`HOME`
design.)

## Defense in depth

Two layers reject everything outside the whitelist; each one alone would
suffice, but together they keep the policy explicit at both Linux- and
shell-level:

1. **sudoers drop-in** [`terminal/Dockerfile`](../../../terminal/Dockerfile)
   adds `/etc/sudoers.d/azureuser-apt`:
   ```
   azureuser ALL=(root) NOPASSWD: /usr/bin/apt-get update
   azureuser ALL=(root) NOPASSWD: /usr/bin/apt-get install *
   azureuser ALL=(root) NOPASSWD: /usr/bin/apt update
   azureuser ALL=(root) NOPASSWD: /usr/bin/apt install *
   ```
   Anything else under `sudo` prompts for a password the operator does
   not have, so it fails closed. Validated at build time with
   `visudo -cf`.

2. **Command guard** [`terminal/command_guard.sh`](../../../terminal/command_guard.sh)
   replaces the unconditional `sudo` block with a leading-command
   whitelist matching the same four `apt`/`apt-get` patterns. Everything
   else with `sudo` returns the new restriction message *before*
   `execve()` and never reaches the sudoers check.

## Diff summary

- `terminal/Dockerfile` — install `sudo` (apt deps) + add
  `/etc/sudoers.d/azureuser-apt` (after the `useradd azureuser` step).
- `terminal/command_guard.sh` — single rule replaced; no public surface
  changes (`__elb_terminal_guard` / `__elb_terminal_command_allowed`
  signatures unchanged).
- `api/tests/test_terminal_command_guard.py` — +12 tests covering the
  new policy (install/update allowed; remove/purge/autoremove/dist-
  upgrade/dpkg/bash/rm/chained-sudo blocked).

## Validation

- `uv run pytest -q api/tests/test_terminal_command_guard.py` → 19 passed.
- `uv run pytest -q api/tests` → 219 passed (was 207; +12 new).
- `uv run ruff check api/tests/test_terminal_command_guard.py` → clean.
- Image rebuild (`docker compose -p elb-control-local build terminal`)
  succeeded; `visudo -cf` validation passed during the build.
- Live exec inside the recreated sidecar:
  ```text
  $ sudo -ln
  User azureuser may run the following commands on …:
      (root) NOPASSWD: /usr/bin/apt-get update
      (root) NOPASSWD: /usr/bin/apt-get install *
      (root) NOPASSWD: /usr/bin/apt update
      (root) NOPASSWD: /usr/bin/apt install *
  $ sudo apt-get update    # 38 MB fetched, OK
  $ sudo apt-get install -y htop
  Setting up htop (3.3.0-4build1) …
  $ htop --version
  htop 3.3.0
  ```
- Block paths in interactive shell (DEBUG trap fires):
  ```text
  $ sudo apt remove curl
  ELB terminal guard blocked: sudo is restricted to 'apt update' and 'apt install' …
  $ sudo bash -c "id"
  ELB terminal guard blocked: sudo is restricted to 'apt update' and 'apt install' …
  $ sudo dpkg -l
  ELB terminal guard blocked: sudo is restricted to 'apt update' and 'apt install' …
  ```

## Notes for reviewers

- Shell-metacharacter injection (`sudo apt install foo; rm -rf /`) is
  not exploitable: sudo invokes `/usr/bin/apt-get install <args>` via
  `execve()` and does not interpret `;`, `&&`, `|`, etc. The trailing
  command is run by the parent bash, where the DEBUG trap re-evaluates
  it through `__elb_terminal_guard` and blocks anything destructive.
- `apt install <local.deb>` is permitted by sudoers; this is the
  documented `apt install` interface and is acceptable for an
  authenticated, tenant-role-checked operator.
- We deliberately did **not** allow `sudo apt-get -o ...`-style global
  option overrides — they are still allowed by sudoers (`*` is
  permissive) but blocked by the leading-command guard regex which
  requires `update` or `install` immediately after `apt[-get]`.
- Production Container App image will pick up the same Dockerfile via
  `az acr build` in `scripts/dev/postprovision.sh`; no template change
  is required.
