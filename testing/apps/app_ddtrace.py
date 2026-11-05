# SPDX-License-Identifier: Apache-2.0

"""Fixture workload that starts Datadog's tracer in code (`import ddtrace.auto`)."""

import ddtrace.auto  # noqa: F401  # isort: skip

from app_asgi import app  # noqa: E402,F401
