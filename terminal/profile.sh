#!/bin/bash
# Sourced into every interactive bash session inside the terminal sidecar.
# Sets the env vars elastic-blast and azcopy expect. Azure CLI uses the
# shared Container App managed identity (AZURE_CLIENT_ID / AZURE_TENANT_ID)
# unless a user explicitly runs 'az login' to authenticate as themselves.

export PYTHONPATH="/opt/elb/runtime_overrides:/opt/elb/elastic-blast-azure/src${PYTHONPATH:+:$PYTHONPATH}"
export AZCOPY_AUTO_LOGIN_TYPE="${AZCOPY_AUTO_LOGIN_TYPE:-AZCLI}"
export ELB_DASHBOARD_FAST_JSON_SUBMIT_CLEANUP="${ELB_DASHBOARD_FAST_JSON_SUBMIT_CLEANUP:-1}"
export ELB_DASHBOARD_FAST_AZURE_IO="${ELB_DASHBOARD_FAST_AZURE_IO:-1}"
export ELB_DASHBOARD_SCOPE_K8S_LOGS="${ELB_DASHBOARD_SCOPE_K8S_LOGS:-1}"
export ELB_SKIP_DB_VERIFY="${ELB_SKIP_DB_VERIFY:-true}"
export ELB_DISABLE_AUTO_SHUTDOWN="${ELB_DISABLE_AUTO_SHUTDOWN:-1}"
export PATH="/opt/elb/venv/bin:/opt/elb/elastic-blast-azure/bin:$PATH"

# Azure CLI automatically uses Managed Identity when AZURE_CLIENT_ID is set.
# The shared container identity is safe for data-plane operations; users can
# override with 'az login --use-device-code' for their own credentials.
# Note: .azure/ cache is ephemeral per session (container restart wipes /home).
export AZURE_ALLOW_NO_SUBSCRIPTIONS=true

if [[ $- == *i* && -z "${ELB_MOTD_SHOWN:-}" ]]; then
  export ELB_MOTD_SHOWN=1
  if [[ -x /usr/local/bin/elb-banner ]]; then
    /usr/local/bin/elb-banner
  elif [[ -r /etc/motd ]]; then
    cat /etc/motd
  fi
fi

if [[ -r /etc/profile.d/elb-command-guard.sh ]]; then
  # shellcheck source=/etc/profile.d/elb-command-guard.sh
  source /etc/profile.d/elb-command-guard.sh
fi
