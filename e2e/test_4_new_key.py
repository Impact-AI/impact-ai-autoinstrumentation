# SPDX-License-Identifier: Apache-2.0

"""AE-10: the key is revoked, the customer installs again with a new one, and snapshots and traces resume.

Cover your AI mints a new key for a reinstall. `helm upgrade --install` with it must replace the gateway and the
reporter, which read the key when they start, so that nobody has to delete a pod to get the cluster reporting again.
"""

from __future__ import annotations

from kube import KEY, call_app, install, pods, running_pod, wait_until

NEW_KEY = "e2e-ingest-key-reinstalled-" + "fedcba9876543210" * 2
ANSWER = {"answer": "Hello from the stub."}


def live_pods(component: str) -> dict[str, str]:
    """uid -> name of the component's pods that are not on their way out."""
    return {
        p["metadata"]["uid"]: p["metadata"]["name"]
        for p in pods("impact-ai", f"app.kubernetes.io/name={component}")
        if not p["metadata"].get("deletionTimestamp")
    }


def refused_routes(world, since: int) -> set[str]:
    return {r["path"] for r in world.mock.requests(since=since) if r.get("status") == 401}


def test_ae10_reinstalling_with_a_new_key_resumes_snapshots_and_spans(world):
    gateway, reporter = live_pods("gateway"), live_pods("reporter")
    assert len(gateway) == len(reporter) == 1
    try:
        # The key the cluster has is revoked: ingest answers 401 on both routes, to the reporter and to the gateway.
        mark = world.mock.last_seq()
        world.mock.control(require_key=NEW_KEY)

        def both_refused():
            assert call_app("shop", "support-assistant", "openai") == ANSWER  # the workload does not notice
            return refused_routes(world, mark) >= {"/v1/inventory", "/v1/traces"}

        wait_until(both_refused, timeout=120, interval=3, message="a 401 on /v1/inventory and on /v1/traces")
        revoked = world.mock.last_seq()

        # The customer installs again, with the new key.
        install(key=NEW_KEY)

        # Both pods were replaced by the install itself, and are the only ones left.
        wait_until(
            lambda: len(live_pods("gateway")) == len(live_pods("reporter")) == 1, timeout=120, message="old pods gone"
        )
        assert not set(live_pods("gateway")) & set(gateway), "the gateway was not replaced by the install"
        assert not set(live_pods("reporter")) & set(reporter), "the reporter was not replaced by the install"
        running_pod("impact-ai", "app.kubernetes.io/name=gateway")
        running_pod("impact-ai", "app.kubernetes.io/name=reporter")

        # Snapshots resume: a snapshot the new reporter sent is accepted.
        wait_until(
            lambda: [
                r
                for r in world.mock.requests("/v1/inventory", since=revoked)
                if r.get("status") == 202 and r.get("json")
            ],
            timeout=90,
            interval=3,
            message="an accepted snapshot after the reinstall",
        )

        # Spans resume: the workload's model call reaches ingest through the new gateway.
        def accepted_llm_span():
            assert call_app("shop", "support-assistant", "openai") == ANSWER
            return [s for s in world.mock.spans(since=revoked) if "gen_ai.request.model" in s["attrs"]]

        wait_until(accepted_llm_span, timeout=90, interval=3, message="an accepted LLM span after the reinstall")
    finally:
        # Leave the suite as it found it: the key every other test installs with.
        world.mock.control(require_key=KEY)
        install(key=KEY)
