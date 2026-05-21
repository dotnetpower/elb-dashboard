# syntax=docker/dockerfile:1.7
# Multi-stage build for the frontend sidecar.
# Stage 1: build the Vite SPA.
# Stage 2: serve via nginx:alpine on loopback :8081 — the api sidecar
#          reverse-proxies non-/api/* requests here.

FROM node:20-alpine AS build
WORKDIR /web

# Install deps with cache friendliness.
COPY web/package.json web/package-lock.json* ./
RUN npm ci

COPY web/ ./

# Vite bakes VITE_* values into the static bundle. Keep committed env files
# generic; production builds pass real values from azd/postprovision as args.
ARG VITE_API_BASE_URL=""
ARG VITE_AUTH_DEV_BYPASS=false
ARG VITE_AZURE_REDIRECT_URI=__RUNTIME__
ARG VITE_AZURE_TENANT_ID=common
ARG VITE_AZURE_CLIENT_ID=""
ARG VITE_FEATURE_CUSTOM_DB=true
ARG VITE_FEATURE_LAB_TOOLS=true
ARG VITE_FEATURE_TERMINAL=true
# Version stamp (host computes via quick-deploy.sh; .git is not in ACR context).
ARG APP_VERSION=""
ARG GIT_COMMIT=""
ARG BUILD_TIME=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL \
    VITE_AUTH_DEV_BYPASS=$VITE_AUTH_DEV_BYPASS \
    VITE_AZURE_REDIRECT_URI=$VITE_AZURE_REDIRECT_URI \
    VITE_AZURE_TENANT_ID=$VITE_AZURE_TENANT_ID \
    VITE_AZURE_CLIENT_ID=$VITE_AZURE_CLIENT_ID \
    VITE_FEATURE_CUSTOM_DB=$VITE_FEATURE_CUSTOM_DB \
    VITE_FEATURE_LAB_TOOLS=$VITE_FEATURE_LAB_TOOLS \
    VITE_FEATURE_TERMINAL=$VITE_FEATURE_TERMINAL \
    APP_VERSION=$APP_VERSION \
    GIT_COMMIT=$GIT_COMMIT \
    BUILD_TIME=$BUILD_TIME
RUN npm run build


FROM nginx:alpine AS runtime

# nginx + python3 (cgroup reporter) under supervisord. We accept the +15MB
# image size to surface near-real-time CPU/MEM for the frontend sidecar
# alongside the other five — without this the dashboard would have a
# permanent "frontend: unknown" gap.
RUN apk add --no-cache python3 py3-pip supervisor \
    && pip3 install --break-system-packages --no-cache-dir redis==5.2.0

# Drop the default site and ship our own config.
RUN rm /etc/nginx/conf.d/default.conf
COPY web/nginx.conf /etc/nginx/conf.d/elb.conf

# SPA bundle.
COPY --from=build /web/dist /usr/share/nginx/html

# Sidecar metrics reporter — see web/cgroup_reporter.py for the contract
# (mirrored from terminal/cgroup_reporter.py).
COPY web/cgroup_reporter.py /usr/local/bin/elb-cgroup-reporter
RUN chmod +x /usr/local/bin/elb-cgroup-reporter

# Process supervisor — runs nginx (primary) + cgroup reporter (advisory).
COPY web/supervisord.conf /etc/supervisord.conf
COPY web/entrypoint.sh /usr/local/bin/elb-frontend-entrypoint
RUN chmod +x /usr/local/bin/elb-frontend-entrypoint

# Nginx and the reporter both run as root inside the container — the
# loopback ingress means nothing from outside ever talks to either of them
# directly, and reading /sys/fs/cgroup/* is unprivileged anyway.
ENV SIDECAR_NAME=frontend
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8081/ || exit 1

ENTRYPOINT ["/usr/local/bin/elb-frontend-entrypoint"]
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
