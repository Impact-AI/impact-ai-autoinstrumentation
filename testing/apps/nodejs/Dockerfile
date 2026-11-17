# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# Fixture workloads for the Node.js image tests: HTTP servers that call model providers and carry no tracing of their own,
# in every library version the qualification covers, as CommonJS and as ES modules. One image per Node (NODE=22|24|26);
# base images are pinned by digest. Each directory of /apps is an application of its own, with its own node_modules from a
# lock file: /apps/openai-5/openai.cjs, /apps/anthropic-1/anthropic.mjs, /apps/express/express.cjs, ...
ARG NODE=22
FROM docker.io/library/node:22-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS node22
FROM docker.io/library/node:24-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS node24
FROM docker.io/library/node:26-slim@sha256:5adad64f9a1fca2d51972caa65983634c8a774eb300d72b20f7f2a0f60368072 AS node26

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
        langsmith) cp /src/langsmith.cjs "$dir" ;; \
        context) cp /src/context.cjs "$dir" ;; \
      esac; \
    done
EXPOSE 8000
