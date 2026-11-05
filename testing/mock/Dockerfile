# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

FROM docker.io/library/python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
COPY --chmod=0644 server.py /srv/server.py
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["python", "/srv/server.py", "--port", "8080"]
