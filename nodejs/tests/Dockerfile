# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# The unit tests of the Node.js package, with the test-only dependencies of tests/package.json (the official encoder as
# the oracle of the exporter, and the SDK that makes its spans). Build context: nodejs/.
#   docker build -f tests/Dockerfile -t unit nodejs && docker run --rm unit
FROM docker.io/library/node:22-slim@sha256:4b150b48b4116e2a22f2c39cc27025fbf4f153e2078fd3b14d159fdb5c7279da
WORKDIR /work
COPY tests/package.json tests/package-lock.json ./tests/
RUN --mount=type=cache,target=/root/.npm cd tests && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error
COPY src ./src
COPY tools ./tools
COPY tests/unit ./tests/unit
CMD ["node", "--test", "--test-reporter=spec", "tests/unit/*.test.js", "tests/unit/*.test.mjs"]
