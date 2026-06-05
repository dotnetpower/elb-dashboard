# syntax=docker/dockerfile:1.7
# Browser-accessible operator shell sidecar.
#
# Image contains:
#   * Ubuntu 24.04 base (python3.12 native)
#   * azure-cli, kubectl, azcopy, python3.12, primer3, tmux, ttyd, jq, git, make
#   * nano, tree, ifconfig, BLAST+, MAFFT, SeqKit, Samtools, BEDTools, and related sequence tools
#   * elastic_blast Python package + venv pre-installed under /opt/elb/venv
#
# Listens on 127.0.0.1:7681 (loopback only). The api sidecar reverse-proxies
# the browser WebSocket (/api/terminal/ws) into this loopback port after MSAL
# + tenant-role check.
#
# Azure CLI login is intentionally user-driven in the interactive shell. The
# operator runs `az login --use-device-code`; azcopy then reuses that Azure CLI
# profile via AZCOPY_AUTO_LOGIN_TYPE=AZCLI.

FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    BLAST_VERSION=2.17.0 \
    HOME=/home/azureuser \
    USER=azureuser \
    SHELL=/bin/bash \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/elb/venv/bin:/opt/elb/elastic-blast-azure/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ---------------------------------------------------------------------------
# Apt deps + Python 3.12 (native to Ubuntu 24.04, no PPA required)
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg lsb-release apt-transport-https software-properties-common \
        sudo \
        git make jq unzip tmux primer3 \
        nano vim-tiny less tree file wget rsync zip gzip pigz tar \
        procps psmisc htop iproute2 net-tools iputils-ping dnsutils \
        mafft seqkit samtools bcftools bedtools fastqc hmmer emboss clustalo muscle \
        build-essential libffi-dev libssl-dev \
        python3.12 python3.12-venv python3.12-dev \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# NCBI BLAST+ (explicitly pinned; apt packages can lag or vary by distro).
# ---------------------------------------------------------------------------
RUN curl --fail --show-error --location --retry 4 --connect-timeout 20 --max-time 300 \
    "https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/${BLAST_VERSION}/ncbi-blast-${BLAST_VERSION}+-x64-linux.tar.gz" \
    -o /tmp/ncbi-blast.tgz \
    && tar -xzf /tmp/ncbi-blast.tgz -C /opt \
    && ln -sf "/opt/ncbi-blast-${BLAST_VERSION}+/bin/"* /usr/local/bin/ \
    && rm -f /tmp/ncbi-blast.tgz

# ---------------------------------------------------------------------------
# azure-cli (apt repo)
# ---------------------------------------------------------------------------
RUN curl --fail --show-error --location --retry 4 --connect-timeout 20 --max-time 120 https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /etc/apt/trusted.gpg.d/microsoft.gpg \
    && AZ_REPO=$(lsb_release -cs) \
    && echo "deb [arch=amd64] https://packages.microsoft.com/repos/azure-cli/ $AZ_REPO main" > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends azure-cli \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# kubectl (direct binary, version-pinned at build time)
# ---------------------------------------------------------------------------
RUN KUBECTL_VERSION=$(curl --fail --show-error --location --retry 4 --connect-timeout 20 --max-time 120 https://dl.k8s.io/release/stable.txt) \
    && curl --fail --show-error --location --retry 4 --connect-timeout 20 --max-time 300 "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

# ---------------------------------------------------------------------------
# azcopy
# ---------------------------------------------------------------------------
RUN curl --fail --show-error --location --retry 4 --connect-timeout 20 --max-time 300 https://aka.ms/downloadazcopy-v10-linux -o /tmp/azcopy.tgz \
    && tar -xf /tmp/azcopy.tgz -C /tmp \
    && install -m 0755 /tmp/azcopy_linux_*/azcopy /usr/local/bin/azcopy \
    && rm -rf /tmp/azcopy*

# ---------------------------------------------------------------------------
# ttyd (precompiled static binary). Falls back to apt build-from-source only
# if the GitHub release URL changes.
# ---------------------------------------------------------------------------
RUN curl --fail --show-error --location --retry 4 --connect-timeout 20 --max-time 300 https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.x86_64 -o /usr/local/bin/ttyd \
    && chmod +x /usr/local/bin/ttyd

# ---------------------------------------------------------------------------
# elastic-blast Python package (pre-installed venv at /opt/elb/venv)
# ---------------------------------------------------------------------------
RUN python3.12 -m venv /opt/elb/venv \
    && /opt/elb/venv/bin/pip install --upgrade pip wheel build setuptools \
    && /opt/elb/venv/bin/pip install \
        azure-mgmt-resource azure-mgmt-network azure-mgmt-compute \
        azure-mgmt-storage azure-mgmt-containerregistry azure-mgmt-containerservice \
        azure-mgmt-authorization azure-mgmt-msi azure-mgmt-monitor \
        azure-identity azure-storage-blob azure-keyvault-secrets \
        redis==5.2.0

# Clone elastic-blast, apply dashboard-specific Azure fixes, and install it
# into the venv. The upstream uses NCBI's `packit` / `pbr` which fights pip's
# PEP 517 build isolation, so install with --no-build-isolation --no-deps
# (matches the legacy cloud-init behavior).
ARG ELASTIC_BLAST_REPO=https://github.com/dotnetpower/elastic-blast-azure.git
ARG ELASTIC_BLAST_REF=master
COPY patch_elastic_blast.py /tmp/patch_elastic_blast.py
COPY merge-sharded-results.sh /tmp/merge-sharded-results.sh
RUN git clone --depth 1 --branch "${ELASTIC_BLAST_REF}" "${ELASTIC_BLAST_REPO}" /opt/elb/elastic-blast-azure \
    && python3.12 /tmp/patch_elastic_blast.py /opt/elb/elastic-blast-azure /tmp/merge-sharded-results.sh \
    && /opt/elb/venv/bin/pip install --no-cache-dir -r /opt/elb/elastic-blast-azure/requirements/base.txt packit glob2 \
    && /opt/elb/venv/bin/pip install --no-build-isolation --no-deps /opt/elb/elastic-blast-azure

# ---------------------------------------------------------------------------
# Operator user + home directory. The Container Apps deployment intentionally
# keeps this container-local; durable query/result files should be staged to
# workload Storage.
# ---------------------------------------------------------------------------
RUN id azureuser 2>/dev/null || useradd -m -s /bin/bash azureuser \
    && mkdir -p /home/azureuser \
    && chown -R azureuser:azureuser /home/azureuser

# ---------------------------------------------------------------------------
# Limited sudoers: azureuser can run ONLY `apt-get install` / `apt-get update`
# (and the `apt` aliases) without a password. Anything else under sudo will
# prompt for a password the operator does not have, which makes sudo a no-op.
# This pairs with the leading-command-only whitelist in command_guard.sh so
# the browser shell can install missing tools (`sudo apt install htop`) but
# cannot remove/purge packages, run `sudo bash`, or escalate generally.
# Pinned absolute paths to defeat $PATH manipulation.
# ---------------------------------------------------------------------------
RUN printf '%s\n' \
        'azureuser ALL=(root) NOPASSWD: /usr/bin/apt-get update' \
        'azureuser ALL=(root) NOPASSWD: /usr/bin/apt-get install *' \
        'azureuser ALL=(root) NOPASSWD: /usr/bin/apt update' \
        'azureuser ALL=(root) NOPASSWD: /usr/bin/apt install *' \
        > /etc/sudoers.d/azureuser-apt \
    && chmod 0440 /etc/sudoers.d/azureuser-apt \
    && visudo -cf /etc/sudoers.d/azureuser-apt

# ---------------------------------------------------------------------------
# Shell environment + MOTD
# Build context is the `terminal/` directory itself, so paths are bare names.
# ---------------------------------------------------------------------------
COPY profile.sh    /etc/profile.d/elb-env.sh
COPY command_guard.sh /etc/profile.d/elb-command-guard.sh
COPY history.sh    /etc/profile.d/elb-history.sh
COPY tmux.conf     /etc/tmux.conf
COPY banner.sh /usr/local/bin/elb-banner
COPY tool-versions.sh /usr/local/bin/elb-tool-versions
COPY motd          /etc/motd
COPY runtime_overrides /opt/elb/runtime_overrides
COPY entrypoint.sh /usr/local/bin/elb-entrypoint
COPY exec_server.py /usr/local/bin/elb-exec-server
COPY cgroup_reporter.py /usr/local/bin/elb-cgroup-reporter
COPY tmux-attach.sh /usr/local/bin/elb-tmux-attach
COPY elb_cfg.py /usr/local/bin/elb-cfg
RUN chmod +x /usr/local/bin/elb-entrypoint /usr/local/bin/elb-exec-server /usr/local/bin/elb-cgroup-reporter /usr/local/bin/elb-banner /usr/local/bin/elb-tool-versions /usr/local/bin/elb-cfg /usr/local/bin/elb-tmux-attach /etc/profile.d/elb-env.sh /etc/profile.d/elb-command-guard.sh /etc/profile.d/elb-history.sh \
    && chmod 0644 /etc/tmux.conf

USER azureuser:azureuser
WORKDIR /home/azureuser
EXPOSE 7681 7682

# ttyd binds to loopback :7681 (interactive browser shell, proxied by api).
# elb-exec-server binds to loopback :7682 (programmatic shell channel for
# Celery tasks; see api/services/terminal_exec.py for the contract).
ENTRYPOINT ["/usr/local/bin/elb-entrypoint"]
