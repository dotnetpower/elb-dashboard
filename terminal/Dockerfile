# syntax=docker/dockerfile:1.7
# Browser-accessible operator shell sidecar.
#
# Image contains:
#   * Ubuntu 22.04 base
#   * azure-cli, kubectl, azcopy, python3.11, primer3, tmux, ttyd, jq, git, make
#   * elastic_blast Python package + venv pre-installed under /opt/elb/venv
#
# Listens on 127.0.0.1:7681 (loopback only). The api sidecar reverse-proxies
# the browser WebSocket (/api/terminal/ws) into this loopback port after MSAL
# + tenant-role check.
#
# Container Apps injects the IDENTITY_ENDPOINT / IDENTITY_HEADER env vars so
# `az login --identity` / `azcopy --auto-login MSI` work without further
# bootstrap.

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/elb/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ---------------------------------------------------------------------------
# Apt deps + Python 3.11
# ---------------------------------------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg lsb-release apt-transport-https software-properties-common \
        git make jq unzip tmux primer3 \
        build-essential libffi-dev libssl-dev \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# azure-cli (apt repo)
# ---------------------------------------------------------------------------
RUN curl -sL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /etc/apt/trusted.gpg.d/microsoft.gpg \
    && AZ_REPO=$(lsb_release -cs) \
    && echo "deb [arch=amd64] https://packages.microsoft.com/repos/azure-cli/ $AZ_REPO main" > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends azure-cli \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# kubectl (direct binary, version-pinned at build time)
# ---------------------------------------------------------------------------
RUN KUBECTL_VERSION=$(curl -sL https://dl.k8s.io/release/stable.txt) \
    && curl -sL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

# ---------------------------------------------------------------------------
# azcopy
# ---------------------------------------------------------------------------
RUN curl -sL https://aka.ms/downloadazcopy-v10-linux -o /tmp/azcopy.tgz \
    && tar -xf /tmp/azcopy.tgz -C /tmp \
    && install -m 0755 /tmp/azcopy_linux_*/azcopy /usr/local/bin/azcopy \
    && rm -rf /tmp/azcopy*

# ---------------------------------------------------------------------------
# ttyd (precompiled static binary). Falls back to apt build-from-source only
# if the GitHub release URL changes.
# ---------------------------------------------------------------------------
RUN curl -sL https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.x86_64 -o /usr/local/bin/ttyd \
    && chmod +x /usr/local/bin/ttyd

# ---------------------------------------------------------------------------
# elastic-blast Python package (pre-installed venv at /opt/elb/venv)
# ---------------------------------------------------------------------------
RUN python3.11 -m venv /opt/elb/venv \
    && /opt/elb/venv/bin/pip install --upgrade pip wheel build \
    && /opt/elb/venv/bin/pip install \
        azure-mgmt-resource azure-mgmt-network azure-mgmt-compute \
        azure-mgmt-storage azure-mgmt-containerregistry azure-mgmt-containerservice \
        azure-mgmt-authorization azure-mgmt-msi azure-mgmt-monitor \
        azure-identity azure-storage-blob azure-keyvault-secrets

# Clone elastic-blast and install it into the venv. The upstream uses NCBI's
# `packit` / `pbr` which fights pip's PEP 517 build isolation, so install
# with --no-build-isolation --no-deps (matches the legacy cloud-init behavior).
RUN git clone --depth 1 https://github.com/dotnetpower/elastic-blast-azure.git /opt/elb/elastic-blast-azure \
    && /opt/elb/venv/bin/pip install --no-build-isolation --no-deps /opt/elb/elastic-blast-azure

# ---------------------------------------------------------------------------
# Operator user + home directory (will be overlaid by the Azure Files mount
# at /home/azureuser at runtime)
# ---------------------------------------------------------------------------
RUN useradd -m -s /bin/bash -u 1000 azureuser \
    && mkdir -p /home/azureuser \
    && chown -R azureuser:azureuser /home/azureuser

# ---------------------------------------------------------------------------
# Shell environment + MOTD
# Build context is the `terminal/` directory itself, so paths are bare names.
# ---------------------------------------------------------------------------
COPY profile.sh    /etc/profile.d/elb-env.sh
COPY motd          /etc/motd
COPY entrypoint.sh /usr/local/bin/elb-entrypoint
RUN chmod +x /usr/local/bin/elb-entrypoint /etc/profile.d/elb-env.sh

USER 1000:1000
WORKDIR /home/azureuser
EXPOSE 7681

# ttyd binds to loopback. The api sidecar reverse-proxies the browser
# WebSocket into this port after MSAL + tenant-role check.
ENTRYPOINT ["/usr/local/bin/elb-entrypoint"]
