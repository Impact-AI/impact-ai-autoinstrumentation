# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# Fixture workloads for the image and end-to-end tests: Python web apps that call model providers and carry no
# tracing of their own. Base images are pinned by digest; PYTHON selects the stage.
ARG PYTHON=3.13
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM docker.io/library/python:3.11-slim@sha256:db20d8506e2f15b6db39b40b6015ca46bae855dec6dd326d462cfe79ae972893 AS py3.11
FROM docker.io/library/python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS py3.12
FROM docker.io/library/python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1 AS py3.13
FROM docker.io/library/python:3.14-slim@sha256:930e1abba7c4e8f96ed93e8012c35d174d7732752731bbd5e8e757d7ec6f3966 AS py3.14
FROM docker.io/library/python:3.12-alpine@sha256:e6dae589c1c05976188fd3bd019afd091445fc533ed2ea29001beddc7368a9c8 AS py3.12-alpine

FROM py${PYTHON} AS app
COPY --from=uv /uv /usr/local/bin/uv
# EXTRA_PACKAGES adds a tracer of the workload's own, for the stand-down tests. REQUIREMENTS picks the locked set:
# requirements-agents.txt adds the agent frameworks (app_agents.py), requirements-langgraph.txt only LangGraph,
# requirements-starlette.txt A returns assistant (app_starlette.py).
ARG EXTRA_PACKAGES=""
ARG REQUIREMENTS=requirements.txt
COPY --chmod=0644 ${REQUIREMENTS} /tmp/requirements.txt
# The download cache is a build mount, not a layer: the images stay small and the builds of one job share the wheels.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system --link-mode=copy --compile-bytecode -r /tmp/requirements.txt ${EXTRA_PACKAGES}
# STUB_DISTRIBUTIONS (name-version, space separated) puts empty distribution metadata for libraries the image cannot trace
# in site-packages. The setup report reads installed distributions and never imports them, which is all a test of it
# needs from CrewAI or LlamaIndex, whose real trees run to hundreds of megabytes.
ARG STUB_DISTRIBUTIONS=""
RUN for dist in ${STUB_DISTRIBUTIONS}; do \
      mkdir -p "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/${dist}.dist-info"; \
    done
ENV AWS_ACCESS_KEY_ID=AKIDSTUB AWS_SECRET_ACCESS_KEY=stub AWS_DEFAULT_REGION=us-east-1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY --chmod=0644 app_*.py run_asgi.py ./
EXPOSE 8000
