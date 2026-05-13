# Primer Design Terminal SSH Execution

## Motivation

Primer Design was too slow when backend execution fell back to Azure VM Run Command. Run Command adds tens of seconds per tool call and caused browser request timeouts. The production Function App also runs on an older Linux glibc, so SSH dependencies needed compatible pins before Paramiko could be used safely.

## User-facing change

Primer Design now executes `primer3_core` on the Remote Terminal VM through SSH-first command execution. Future terminal VMs install `primer3` during cloud-init and listen for SSH on ports 22 and 443. The backend prefers SSH on 443, then tries 22, and only falls back to Azure Run Command if SSH fails.

## API/IaC diff summary

- `compute.run_shell()` now tries SSH on port 443 before port 22.
- `ensure_ssh_from_function_app()` records the Function App's live public egress IP and maintains narrow `/32` NSG entries for ports 22 and 443.
- Primer Design and custom database build pass the terminal password into `run_shell()` so they can use SSH.
- SSH dependencies are pinned to Azure Functions glibc-compatible versions.
- Terminal cloud-init installs `primer3` and writes an sshd override for ports 22 and 443 with password authentication enabled.

## Validation evidence

- Backend syntax check passed for touched modules with `python -m py_compile`.
- Production API deployed successfully as `funcapp-202605140029.zip`; `/api/health` returned 200.
- Existing terminal VM updated to listen on 443; `sshd` showed listeners on ports 22 and 443.
- Browser-originated `POST /api/blast/primer-design` returned HTTP 200 through the Static Web App.
- Repeated Primer Design request time dropped from about 38-46 s with Run Command fallback to about 2.7 s over SSH.
- VM auth logs showed accepted SSH logins from the Function App live egress IPs.
