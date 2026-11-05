# SPDX-License-Identifier: Apache-2.0

"""Measure the overhead of the injected instrumentation (AE-17) on the real images, in containers.

One round, for one combination of runtime and server:

  1. The init container's copy of the payload into a fresh volume is timed (the Operator does this before every
     injected pod). It is published beside the start time and not judged: on a workstation it is mostly the container
     engine starting a container, and pulling the image, which a cluster does on the node, is no part of it.
  2. The workload without injection and the same workload with it are started side by side, a Python one each pinned to
     a core of its own where the engine allows it (a Node.js one is left on all the cores, see cpus_for), each with a
     mock provider (and, for the injected one, the mock is also the gateway). Start is measured from `run` to the first
     answer on the probe.
  3. Both answer 100 calls (warm-up), then the cgroup's CPU time is read, both answer 300 more, the injected side's
     spans are waited for (the exporter's batch delay is the production default), and the cgroup is read again. CPU per
     call is the difference over 300 calls; memory is the working set at the end (memory.current less the inactive file
     cache, which is what Kubernetes counts). A Node.js process is first asked to collect its garbage, on both sides
     alike (see settle): the working set of a garbage-collected heap is mostly what it has not yet let go of.

The cgroup is read from inside each container (`exec`), so that Docker and Podman, rootful and rootless, are all the
same.
"""

from __future__ import annotations

import http.client
import importlib.util
import os
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gate
from combos import Combo

ROOT = Path(__file__).resolve().parents[3]
IMAGE_TESTS = ROOT / "python" / "tests" / "image"
sys.path.insert(0, str(IMAGE_TESTS))

from containers import Container, Mock, Network, run  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The environments the Operator injects, as the image tests keep them (one place for each language).
python_env = _load("overhead_python_env", IMAGE_TESTS / "conftest.py")
node_env = _load("overhead_node_env", ROOT / "nodejs" / "tests" / "image" / "nodejs_support.py")

WARMUP = int(os.environ.get("OVERHEAD_WARMUP", "100"))
CALLS = int(os.environ.get("OVERHEAD_CALLS", "300"))
FLUSH_TIMEOUT = 20.0  # the exporter's batch delay is 5 s
SETTLE_WAIT = 1.0  # seconds a process is given to collect its garbage before the memory is read
SETTLE_STEP = 0.25  # seconds between two readings of the memory while it settles
SETTLE_QUIET = 256 * 1024  # bytes: a reading within this of the one before it is the same reading
SETTLE_TIMEOUT = 30.0  # seconds after which a memory that is still moving is an error
MIB = 1048576.0


class Setup:
    """What a combination needs of the kit's images, and how the Operator injects it."""

    def __init__(self, combo: Combo):
        if combo.runtime == "python":
            self.kit = python_env.IMAGE
            self.workload = python_env.app_image(combo.version)
            self.mount = python_env.MOUNT
            self.source = "/autoinstrumentation/."
            self.injection = {
                key: value for key, value in python_env.OPERATOR_ENV.items() if key != "OTEL_BSP_SCHEDULE_DELAY"
            }
        else:
            self.kit = node_env.IMAGE
            self.workload = node_env.app_image(combo.version)
            self.mount = node_env.MOUNT
            self.source = "/autoinstrumentation/."
            self.injection = {
                key: value for key, value in node_env.OPERATOR_ENV.items() if key != "OTEL_BSP_SCHEDULE_DELAY"
            }
            self.injection["NODE_OPTIONS"] = f"--require {node_env.PRELOAD}"
        self.mock_image = python_env.MOCK_IMAGE

    def images(self) -> list[str]:
        return [self.kit, self.workload, self.mock_image]


def pinning_available(image: str) -> bool:
    """Whether the engine lets a container be pinned to a core (a rootless engine without the cpuset controller does
    not)."""
    cpus = sorted(os.sched_getaffinity(0))
    result = run("run", "--rm", "--cpuset-cpus", str(cpus[0]), "--entrypoint", "true", image, check=False)
    return result.returncode == 0


def pick_cpus() -> tuple[int, int] | None:
    """Two cores that are not hyperthreads of one another, from those the process may use."""
    cpus = sorted(os.sched_getaffinity(0))
    if len(cpus) < 4:
        return None

    def siblings(cpu: int) -> set[int]:
        try:
            text = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list").read_text().strip()
        except OSError:
            return {cpu}
        found: set[int] = set()
        for part in text.split(","):
            low, _, high = part.partition("-")
            found.update(range(int(low), int(high or low) + 1))
        return found

    first = cpus[len(cpus) // 4]
    for candidate in reversed(cpus):
        if candidate != first and candidate not in siblings(first):
            return first, candidate
    return None


def cpus_for(combo: Combo, *, pinning: bool) -> tuple[int, int] | None:
    """The two cores a combination's two sides are pinned to, or None for neither.

    A Python workload is pinned, one core per side, so that the sides do not share one and the CPU time per call is
    quiet. A Node.js workload is not: V8 runs its garbage collector in threads beside the main one, on a single pinned
    core they starve, and the heap grows before they catch up. The memory of the two sides then differs by whatever the
    collector was doing when it was read: between 14 and 49 MiB with one core each on one machine, and between -12 and
    +22 with the cores free, where the collector keeps up as it does in a cluster.
    """
    if not pinning or combo.runtime == "node":
        return None
    return pick_cpus()


def read_cgroup(container: Container) -> tuple[int, int]:
    """(CPU time in microseconds, working set in bytes) of the container's cgroup, read from inside it."""
    script = (
        "if [ -f /sys/fs/cgroup/cpu.stat ]; then "
        "awk '$1==\"usage_usec\"{print $2}' /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/memory.current; "
        "awk '$1==\"inactive_file\"{print $2}' /sys/fs/cgroup/memory.stat; "
        "else echo $(( $(cat /sys/fs/cgroup/cpuacct/cpuacct.usage) / 1000 )); "
        "cat /sys/fs/cgroup/memory/memory.usage_in_bytes; "
        "awk '$1==\"total_inactive_file\"{print $2}' /sys/fs/cgroup/memory/memory.stat; fi"
    )
    out = run("exec", container.name, "sh", "-c", script).stdout.split()
    usage, current, inactive = int(out[0]), int(out[1]), int(out[2]) if len(out) > 2 else 0
    return usage, max(current - inactive, 0)


def settle(containers: Sequence[Container], signal: str) -> dict[str, int]:
    """The working set, in bytes, of each container once its process has collected all its garbage.

    The working set of a garbage-collected heap is mostly what the process has not yet let go of, and V8 lets go of
    little until it is short of memory. Its young generation grows by doubling while enough survives a scavenge (to 32
    MiB on Node 22, to 64 or 128 MiB on Node 24 and 26) and does not shrink on its own, so after a few hundred calls
    the working set shows how far that growth has got, not what the process needs. Two identical plain workloads
    differ by -22 to +23 MiB (Node 22), and the injected one, which is further along, reads +33 to +46 MiB above the
    plain one on Node 24 and 26. With ten times the calls both have grown to the same size and the difference is +6 to
    +11 MiB on Node 22, 24 and 26, within 3 MiB of what it is after a collection: that is the kit's. What the kit
    costs is what a process holds when nothing waits to be freed: its live heap, its code and its buffers. So both
    sides are asked, at the same moment and the same way, to collect as V8 does when memory is short (`signal`:
    testing/apps/nodejs/src/settle.cjs), and the memory is read when it has stopped falling. The reading before the
    collection is kept beside it (Side.memory_unsettled_mib): published, not judged.
    """
    for container in containers:
        run("kill", "--signal", signal, container.name)
    time.sleep(SETTLE_WAIT)
    deadline = time.monotonic() + SETTLE_TIMEOUT
    last = {c.name: read_cgroup(c)[1] for c in containers}
    while time.monotonic() < deadline:
        time.sleep(SETTLE_STEP)
        now = {c.name: read_cgroup(c)[1] for c in containers}
        if all(abs(now[name] - last[name]) <= SETTLE_QUIET for name in now):
            return now
        last = now
    names = ", ".join(last)
    raise AssertionError(f"the memory of {names} was still moving {SETTLE_TIMEOUT:.0f} s after the collection")


def wait_for_probe(container: Container, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection("127.0.0.1", container.port, timeout=2)
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            response.read()
            connection.close()
            if response.status == 200:
                return
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.025)
    raise AssertionError(f"{container.name} did not answer its probe in {timeout:.0f} s\n{container.logs()[-2000:]}")


def drive(container: Container, calls: int) -> None:
    """`calls` model calls through the workload, one after another on one connection."""
    connection = http.client.HTTPConnection("127.0.0.1", container.port, timeout=60)
    body = b'{"prompt": "hi there"}'
    headers = {"Content-Type": "application/json"}
    try:
        for _ in range(calls):
            connection.request("POST", "/chat/openai", body, headers)
            response = connection.getresponse()
            payload = response.read()
            if response.status != 200:
                raise AssertionError(
                    f"{container.name}: HTTP {response.status} {payload[:200]!r}\n{container.logs()[-2000:]}"
                )
    finally:
        connection.close()


def llm_spans(mock: Mock) -> int:
    return sum(
        1
        for span in mock.spans()
        if span["name"] != "impact.setup" and any(key.startswith(("gen_ai.", "llm.")) for key in span["attrs"])
    )


def wait_for_spans(mock: Mock, expected: int) -> None:
    """Until `expected` model-call spans have arrived (the exporter's batch delay is 5 s), and a moment longer."""
    deadline = time.monotonic() + FLUSH_TIMEOUT
    while llm_spans(mock) < expected and time.monotonic() < deadline:
        time.sleep(0.5)
    time.sleep(0.5)


class Session:
    """Containers and volumes of a measurement, removed when it ends."""

    def __init__(self, combo: Combo):
        self.combo = combo
        self.setup = Setup(combo)
        self.network = Network.create()
        self.volumes: list[str] = []
        self.containers: list[Container] = []
        self.pinned = pinning_available(self.setup.mock_image)
        self.cpus = cpus_for(combo, pinning=self.pinned)

    def close(self) -> None:
        for container in self.containers:
            container.stop()
        for volume in self.volumes:
            run("volume", "rm", "-f", volume, check=False)
        self.network.remove()

    def mock(self) -> Mock:
        container = Container.start(
            self.setup.mock_image, network=self.network, publish=8080, name=f"i019-mock-{uuid.uuid4().hex[:6]}"
        )
        self.containers.append(container)
        return Mock(container)

    def copy_payload(self) -> tuple[str, float]:
        """The init container's copy into a fresh volume, as the Operator makes it: the volume and how long the copy
        took."""
        volume = f"i019-overhead-{uuid.uuid4().hex[:8]}"
        self.volumes.append(volume)
        run("volume", "create", volume)
        # A new volume belongs to root and is not writable by the image's user; a Kubernetes emptyDir is (mode 1777).
        run(
            "run",
            "--rm",
            "--user",
            "0",
            "-v",
            f"{volume}:{self.setup.mount}",
            self.setup.kit,
            "chmod",
            "1777",
            self.setup.mount,
        )
        started = time.monotonic()
        run(
            "run",
            "--rm",
            "-v",
            f"{volume}:{self.setup.mount}",
            self.setup.kit,
            "cp",
            "-r",
            self.setup.source,
            self.setup.mount,
        )
        return volume, time.monotonic() - started

    def start_side(self, mock: Mock, *, injected: bool, volume: str | None, cpu: int | None) -> tuple[Container, float]:
        env = {"STUB_URL": mock.internal_url}
        if injected:
            env.update(self.setup.injection)
            env["IMPACT_OTLP_ENDPOINT"] = mock.internal_url
        extra = ["--cpuset-cpus", str(cpu)] if cpu is not None else []
        if self.combo.directory:
            extra += ["-w", self.combo.directory]
        started = time.monotonic()
        container = Container.start(
            self.setup.workload,
            network=self.network,
            command=list(self.combo.command),
            env=env,
            volumes={volume: self.setup.mount} if injected and volume else None,
            publish=8000,
            user="10001:10001",  # not root, and not the user that copied the files
            extra_args=extra,
        )
        self.containers.append(container)
        wait_for_probe(container)
        return container, time.monotonic() - started

    def one_round(self) -> gate.Round:
        plain_mock, injected_mock = self.mock(), self.mock()
        volume, copy_seconds = self.copy_payload()
        cpus = self.cpus or (None, None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            plain_future = pool.submit(self.start_side, plain_mock, injected=False, volume=None, cpu=cpus[0])
            injected_future = pool.submit(self.start_side, injected_mock, injected=True, volume=volume, cpu=cpus[1])
            (plain, plain_start), (injected, injected_start) = plain_future.result(), injected_future.result()

            self.both(pool, plain, injected, WARMUP)
            # the warm-up's spans are exported before the window opens, so that their export is not counted in it
            wait_for_spans(injected_mock, WARMUP)
            before = {c.name: read_cgroup(c) for c in (plain, injected)}
            self.both(pool, plain, injected, CALLS)
            # the injected side's spans go out in the exporter's batches: wait until all of them have, so that their
            # CPU is in
            wait_for_spans(injected_mock, WARMUP + CALLS)
            after = {c.name: read_cgroup(c) for c in (plain, injected)}
            settled = settle((plain, injected), self.combo.settle) if self.combo.settle else {}
        assert llm_spans(injected_mock) >= WARMUP + CALLS, (
            f"{self.combo.id}: {llm_spans(injected_mock)} of {WARMUP + CALLS} model-call spans arrived\n"
            f"{injected.logs()[-2000:]}"
        )
        assert llm_spans(plain_mock) == 0, "the workload without injection sent spans"

        def side(container: Container, start: float, init: float = 0.0) -> gate.Side:
            cpu = (after[container.name][0] - before[container.name][0]) / 1000.0 / CALLS
            unsettled = after[container.name][1]
            return gate.Side(
                cpu_ms_per_call=cpu,
                memory_mib=settled.get(container.name, unsettled) / MIB,
                start_s=start,
                init_s=init,
                memory_unsettled_mib=unsettled / MIB if settled else None,
            )

        measured = gate.Round(plain=side(plain, plain_start), injected=side(injected, injected_start, copy_seconds))
        for container in (plain, injected, *[c for c in self.containers if c.name.startswith("i019-mock-")]):
            container.stop()
        run("volume", "rm", "-f", volume, check=False)
        return measured

    @staticmethod
    def both(pool: ThreadPoolExecutor, plain: Container, injected: Container, calls: int) -> None:
        errors: list[BaseException] = []

        def work(container: Container) -> None:
            try:
                drive(container, calls)
            except BaseException as error:  # noqa: BLE001 - re-raised below, on the caller's thread
                errors.append(error)

        threads = [threading.Thread(target=work, args=(c,)) for c in (plain, injected)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise errors[0]


def measure(combo: Combo, rounds: int) -> tuple[list[gate.Round], dict]:
    """`rounds` paired rounds of one combination: the rounds, and what was true of the machine they ran on."""
    session = Session(combo)
    try:
        measured = [session.one_round() for _ in range(rounds)]
        return measured, {"pinned": session.cpus is not None, "cpus": list(session.cpus) if session.cpus else None}
    finally:
        session.close()
