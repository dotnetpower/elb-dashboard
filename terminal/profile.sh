#!/bin/bash
# Sourced into every interactive bash session inside the terminal sidecar.
# Sets the env vars elastic-blast and azcopy expect, and tries an MI login
# if no Azure account is active yet.

export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}/opt/elb/elastic-blast-azure/src"
export AZCOPY_AUTO_LOGIN_TYPE="${AZCOPY_AUTO_LOGIN_TYPE:-MSI}"
export ELB_SKIP_DB_VERIFY="${ELB_SKIP_DB_VERIFY:-true}"
export ELB_DISABLE_AUTO_SHUTDOWN="${ELB_DISABLE_AUTO_SHUTDOWN:-1}"
export PATH="/opt/elb/venv/bin:$PATH"

# Best-effort `az login --identity` so the operator does not need to type it.
# Container Apps exposes IDENTITY_ENDPOINT + IDENTITY_HEADER. If the account
# already shows, do nothing.
if [ -n "${IDENTITY_ENDPOINT:-}" ] && [ -n "${IDENTITY_HEADER:-}" ]; then
  if ! az account show -o none >/dev/null 2>&1; then
    if [ -n "${AZURE_CLIENT_ID:-}" ]; then
      timeout 15 az login --identity --username "$AZURE_CLIENT_ID" --allow-no-subscriptions -o none >/dev/null 2>&1 || true
    else
      timeout 15 az login --identity --allow-no-subscriptions -o none >/dev/null 2>&1 || true
    fi
  fi
fi
