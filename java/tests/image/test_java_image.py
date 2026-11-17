# SPDX-License-Identifier: Apache-2.0

"""The Java image: the layout the Operator expects, the pinned upstream agent inside it, the licenses of what it
bundles, and the scope cut to request roots and AI."""

from __future__ import annotations

import hashlib
import uuid
import zipfile

import pytest
from java_support import (
    IMAGE,
    ROOT,
    modules,
    pinned_agent_sha256,
    require_images,
    run,
    scope,
    wait_ready,
)

pytestmark = pytest.mark.java


@pytest.fixture(scope="module")
def jar(tmp_path_factory):
    """/javaagent.jar of the image, copied out."""
    require_images(IMAGE)
    name = f"kit-test-java-{uuid.uuid4().hex[:6]}"
    run("create", "--name", name, IMAGE, "true")
    target = tmp_path_factory.mktemp("java") / "javaagent.jar"
    try:
        run("cp", f"{name}:/javaagent.jar", str(target))
    finally:
        run("rm", "-f", name, check=False)
    return target


def test_the_image_is_the_operators_layout_and_runs_as_non_root():
    out = run("run", "--rm", "--entrypoint", "sh", IMAGE, "-c", "id -u; ls /; ls -l /javaagent.jar").stdout
    assert out.splitlines()[0] == "65532"
    assert "javaagent.jar" in out.split()
    assert "-rw-r--r--" in out  # readable by any user the init container runs as


def test_the_operators_copy_works_as_non_root(payload):
    # The fixture ran `cp /javaagent.jar /otel-auto-instrumentation-java/javaagent.jar` as the image's user.
    out = run("run", "--rm", "--entrypoint", "ls", "-v", f"{payload}:/x", IMAGE, "-l", "/x/javaagent.jar").stdout
    assert "javaagent.jar" in out


def test_the_nested_agent_is_the_pinned_upstream_release_byte_for_byte(jar):
    with zipfile.ZipFile(jar) as archive:
        nested = archive.getinfo("impact/opentelemetry-javaagent.jar")
        assert nested.compress_type == zipfile.ZIP_STORED
        agent = archive.read(nested)
        settings = dict(
            line.split("=", 1)
            for line in archive.read("ai/impact/instrumentation/javaagent/impact.properties").decode().splitlines()
            if "=" in line and not line.startswith("#")
        )
        manifest = archive.read("META-INF/MANIFEST.MF").decode()
        extension = archive.read("impact/impact-extension.jar")
        # the launcher is the only Premain-Class: no upstream class is at the top, to shadow a workload's own agent
        assert "io/opentelemetry/javaagent/OpenTelemetryAgent.class" not in archive.namelist()
    assert hashlib.sha256(agent).hexdigest() == pinned_agent_sha256() == settings["agentSha256"]
    assert hashlib.sha256(extension).hexdigest() == settings["extensionSha256"]
    assert settings["javaVersions"] == "17,21,25"
    assert settings["instrumentations"].split(",") == scope()
    assert "Premain-Class: ai.impact.instrumentation.javaagent.ImpactAgent" in manifest
    assert "Can-Retransform-Classes: true" in manifest


def test_the_java_versions_are_a_build_parameter(tmp_path):
    """JAVA_VERSIONS chooses the range: a decision on the versions is a build argument, not a code change."""
    image = f"localhost/impact-instrumentation/java-range-test:{uuid.uuid4().hex[:6]}"
    root = str(ROOT)
    try:
        run(
            "build", "-q", "-f", "java/Dockerfile", "-t", image, "--build-context", f"root={root}",
            "--build-arg", "JAVA_VERSIONS=11,17", f"{root}/java", timeout=900,
        )  # fmt: skip
        name = f"kit-test-java-{uuid.uuid4().hex[:6]}"
        run("create", "--name", name, image, "true")
        try:
            run("cp", f"{name}:/javaagent.jar", str(tmp_path / "javaagent.jar"))
        finally:
            run("rm", "-f", name, check=False)
    finally:
        run("rmi", "-f", image, check=False)
    with zipfile.ZipFile(tmp_path / "javaagent.jar") as archive:
        text = archive.read("ai/impact/instrumentation/javaagent/impact.properties").decode()
    assert "javaVersions=11,17" in text.splitlines()


def test_the_licenses_of_what_it_bundles_are_in_the_image():
    out = run(
        "run", "--rm", "--entrypoint", "sh", IMAGE, "-c",
        "ls /licenses; ls /licenses/java/opentelemetry-javaagent/META-INF/licenses; ls /licenses/busybox",
    ).stdout.split()  # fmt: skip
    for name in ("LICENSE", "NOTICE", "licenses.md", "COPYING"):
        assert name in out, out
    assert any(name.startswith("byte-buddy") for name in out), out


# Names of java/scope.txt that are not the first name of their module, which the agent logs: the module's class instead.
ALIASES = {
    "aws-sdk-2.2-core": "awssdk.v2_2.AwsSdkInstrumentationModule",
    "aws-sdk-2.2-bedrock-runtime": "awssdk.v2_2.BedrockRuntimeInstrumentationModule",
}


def test_the_scope_is_request_roots_clients_propagation_and_ai_and_nothing_else(
    start_app,
):
    """Every name of java/scope.txt is an instrumentation the agent has and turns on; the logging appenders, the
    database and messaging instrumentations and the runtime metrics are off, whatever the pod says."""
    app = start_app(
        "21",
        env={
            # the pod cannot widen the scope, by a name of its own or by one that starts like a name of the scope
            "OTEL_INSTRUMENTATION_JDBC_ENABLED": "true",
            "OTEL_INSTRUMENTATION_REACTOR_KAFKA_ENABLED": "true",
            "OTEL_INSTRUMENTATION_AWS_SDK_ENABLED": "true",
            "OTEL_INSTRUMENTATION_COMMON_DEFAULT_ENABLED": "true",
        },
        tool_options="-Dotel.javaagent.debug=true",
    )
    wait_ready(app)
    logs = app.logs()
    loaded = modules(logs)
    for name in scope():
        if name in ALIASES:
            assert loaded.get(ALIASES[name]) is True, f"{name}: {ALIASES[name]} is not on"
            continue
        assert f"Loading instrumentation {name} " in logs, f"the agent has no instrumentation named {name}"
        assert f"Instrumentation {name} is disabled" not in logs, name
    for name in (
        "jdbc",
        "logback-appender",
        "log4j-appender",
        "java-util-logging",
        "jboss-logmanager-appender",
        "kafka-clients",
        "reactor-kafka",
        "micrometer",
    ):
        assert f"Instrumentation {name} is disabled" in logs, name
    # Bedrock is on; the AWS SDK's messaging and Lambda modules, which also answer to `aws-sdk`, are off
    for module in (
        "awssdk.v2_2.SqsInstrumentationModule",
        "awssdk.v2_2.SnsInstrumentationModule",
        "awssdk.v2_2.LambdaInstrumentationModule",
        "awssdk.v1_11.SqsInstrumentationModule",
        "awssdk.v1_11.AwsSdkInstrumentationModule",
    ):
        assert loaded.get(module) is False, module
