# API submit terminal managed identity fallback

## Motivation

Clean end-to-end validation showed that `/api/blast/submit` could enqueue a Celery task and upload the inline FASTA, but the worker failed when `elastic-blast` ran in the terminal sidecar because Azure CLI had no account session:

```
ERROR: Please run 'az login' to setup account.
```

The API submit path must not require the researcher to manually open the browser terminal before an API-triggered job can run.

## User-facing change

API/Celery BLAST submissions now ensure the terminal exec server has an Azure CLI account before invoking `elastic-blast submit`. If no account is present, the task logs in with the Container App user-assigned managed identity.

The interactive browser terminal remains user-owned. The terminal exec server uses a separate temporary `AZURE_CONFIG_DIR`, so managed-identity login for background automation does not populate `/home/azureuser/.azure`.

## API/IaC diff summary

- `api.tasks.blast.submit` checks `az account show` through `terminal_exec` before `elastic-blast submit`.
- On missing CLI login, the task runs `az login --identity --username $AZURE_CLIENT_ID` through the terminal exec server.
- `terminal/entrypoint.sh` starts `elb-exec-server` with `AZURE_CONFIG_DIR=/tmp/elb-exec-azure` by default, separate from the interactive ttyd shell.

## Validation evidence

- `uv run pytest -q api/tests/test_blast_tasks.py -q`
- `uv run ruff check api/tasks/blast.py api/tests/test_blast_tasks.py`
- `bash -n terminal/entrypoint.sh`

End-to-end deployed API submit validation is in progress against `rg-elbverify0517`.