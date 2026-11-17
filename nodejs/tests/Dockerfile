# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# The unit tests of the Node.js package, with the test-only dependencies of tests/package.json (the official encoder as
# the oracle of the exporter, and the SDK that makes its spans). Build context: nodejs/.
#   docker build -f tests/Dockerfile -t unit nodejs && docker run --rm unit
FROM docker.io/library/node:22-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d
WORKDIR /work
COPY tests/package.json tests/package-lock.json ./tests/
RUN --mount=type=cache,target=/root/.npm cd tests && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error
COPY src ./src
COPY tools ./tools
COPY tests/unit ./tests/unit
CMD ["node", "--test", "--test-reporter=spec", "tests/unit/*.test.js", "tests/unit/*.test.mjs"]
