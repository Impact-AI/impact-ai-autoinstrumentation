# SPDX-License-Identifier: Apache-2.0

"""Proof first: a .NET the kit is not proved on starts as it would without the kit.

The Operator injects every .NET workload the same way, and the kit's hook comes first in DOTNET_STARTUP_HOOKS. A
startup hook that cannot load stops the application, and one built for a newer .NET than the workload's cannot load
(System.Runtime of its version is not there). So the hook is built for net8.0, the oldest .NET the upstream agent
supports, and decides by the .NET it finds: on one the kit is not proved on (DOTNET_VERSIONS) it keeps the upstream
agent from recording anything and says why. It cannot stop the agent there as it does beside another tracer: the
upstream profiler, attached as the process starts, redirects the application's System.Diagnostics.DiagnosticSource 8.0
to the agent's 10.0, which only the agent's loader provides, so the application would not start with the loader
excluded. It turns the agent's signals off instead. The service here runs on .NET 8.
"""

from __future__ import annotations

import time

import pytest
from dotnet_support import DOTNETS, OLDER_IMAGE, agent_spans, http, require_images, wait_ready

pytestmark = pytest.mark.dotnet


def test_a_dotnet_the_kit_is_not_proved_on_starts_without_the_kit(start_app, mock):
    if "8.0" in DOTNETS:
        pytest.skip("the kit is proved on .NET 8 in this run")
    require_images(OLDER_IMAGE)
    app = start_app(image=OLDER_IMAGE)
    wait_ready(app)
    status, answer = http("POST", f"{app.url}/chat", {})
    assert status == 200 and "Hello from .NET 8.0." in str(answer), (status, answer, app.logs()[-3000:])
    time.sleep(2)
    logs = app.logs()
    assert (
        f"impact.ai: not started: .NET 8.0, where the kit is proved on {', '.join(DOTNETS)}; "
        "the upstream agent's signals are off, because its profiler is attached"
    ) in logs, logs[-5000:]
    assert agent_spans(mock) == []
