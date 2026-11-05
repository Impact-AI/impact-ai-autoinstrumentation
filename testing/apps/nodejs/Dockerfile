# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# Fixture workloads for the Node.js image tests: HTTP servers that call model providers and carry no tracing of their own,
# in every library version the qualification covers, as CommonJS and as ES modules. One image per Node (NODE=22|24|26);
# base images are pinned by digest. Each directory of /apps is an application of its own, with its own node_modules from a
# lock file: /apps/openai-5/openai.cjs, /apps/anthropic-1/anthropic.mjs, /apps/express/express.cjs, ...
ARG NODE=22
FROM docker.io/library/node:22-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da AS node22
FROM docker.io/library/node:24-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS node24
FROM docker.io/library/node:26-slim@sha256:e4db4c914bf8ad241bd24804fc25d272d89bad97e68cdc8a1414862790a02c39 AS node26

FROM node${NODE} AS app
WORKDIR /apps
COPY --chmod=0755 versions ./
COPY --chmod=0755 src /src
RUN --mount=type=cache,target=/root/.npm \
    for dir in /apps/*/; do \
      name="$(basename "$dir")"; \
      (cd "$dir" && npm ci --ignore-scripts --omit=dev --no-audit --no-fund --loglevel=error) || exit 1; \
      case "$name" in \
        openai-*) cp /src/openai.cjs /src/openai.mjs "$dir" ;; \
        anthropic-*) cp /src/anthropic.cjs /src/anthropic.mjs "$dir" ;; \
        express) cp /src/express.cjs "$dir" ;; \
        own-otel) cp /src/openai.cjs /src/own-otel.cjs "$dir" ;; \
      esac; \
    done
EXPOSE 8000
