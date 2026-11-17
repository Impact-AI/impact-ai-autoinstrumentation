# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# The Datadog harness's images, one per target: the tracer cells (python, nodejs, java), each running the canonical
# interaction and the provider calls with Datadog's tracer at its pinned version, and `tools`, the standard-library
# doubles around the real Datadog Agent (backend.py, agent_tap.py, canonical_provider.py). Base images and downloads
# are pinned by digest.
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv
FROM docker.io/library/python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS py
FROM docker.io/library/node:22-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS node
FROM docker.io/library/eclipse-temurin:21-jdk@sha256:ebfbb18d11ff163ee83b21258acf12248af48d22a0fdfdc68fa1a1516c05976a AS jdk

FROM py AS tools
ENV PYTHONUNBUFFERED=1
WORKDIR /srv
COPY --chmod=0644 backend.py agent_tap.py canonical_provider.py ./

FROM py AS python
COPY --from=uv /uv /usr/local/bin/uv
COPY --chmod=0644 requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --link-mode=copy --require-hashes -r /tmp/requirements.txt
ENV PYTHONUNBUFFERED=1
WORKDIR /srv
COPY --chmod=0644 canonical.py providers.py ./

FROM node AS nodejs
WORKDIR /srv
COPY --chmod=0644 package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY --chmod=0644 canonical.cjs providers.cjs ./

FROM jdk AS java
WORKDIR /srv
ADD --checksum=sha256:86c97c1348277bb024f6185c4fd15a5567c50ab648f88277dffc429adb4bf927 --chmod=0644 \
    https://repo1.maven.org/maven2/com/datadoghq/dd-java-agent/1.68.1/dd-java-agent-1.68.1.jar dd-java-agent.jar
ADD --checksum=sha256:b09e3c89a8376e6703804b92991cfa0a4f554479af863d09c3bea98efef41ab9 --chmod=0644 \
    https://repo1.maven.org/maven2/com/datadoghq/dd-trace-api/1.68.1/dd-trace-api-1.68.1.jar dd-trace-api.jar
COPY --chmod=0644 Canonical.java ./
