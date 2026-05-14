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
RUN npm run build


FROM nginx:alpine AS runtime

# Drop the default site and ship our own config.
RUN rm /etc/nginx/conf.d/default.conf
COPY web/nginx.conf /etc/nginx/conf.d/elb.conf

# SPA bundle.
COPY --from=build /web/dist /usr/share/nginx/html

# Run as the existing nginx user (uid 101 in alpine).
EXPOSE 8081
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://127.0.0.1:8081/ || exit 1
