#!/bin/bash
# Sourced into every interactive bash session inside the terminal sidecar.
# Sets the env vars elastic-blast and azcopy expect. Azure CLI login is
# deliberately user-driven so terminal activity is attributable to the person
# operating the browser session, not the shared Container App identity.

export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}/opt/elb/elastic-blast-azure/src"
export AZCOPY_AUTO_LOGIN_TYPE="${AZCOPY_AUTO_LOGIN_TYPE:-AZCLI}"
export ELB_SKIP_DB_VERIFY="${ELB_SKIP_DB_VERIFY:-true}"
export ELB_DISABLE_AUTO_SHUTDOWN="${ELB_DISABLE_AUTO_SHUTDOWN:-1}"
export PATH="/opt/elb/venv/bin:/opt/elb/elastic-blast-azure/bin:$PATH"

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
