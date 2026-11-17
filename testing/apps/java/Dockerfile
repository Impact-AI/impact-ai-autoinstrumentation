# syntax=docker/dockerfile:1
# SPDX-License-Identifier: Apache-2.0

# Fixture workloads for the Java image tests: one image per Java (JAVA=11|17|21|25), each with
#   /app/app.jar                          a plain JDK HTTP server and client, no tracing of its own (src/fixture/App.java)
#   /agents/dd-java-agent.jar             a stand-in for an APM vendor's agent (agent/fakeapm/Agent.java)
#   /agents/opentelemetry-javaagent.jar   the upstream OpenTelemetry agent, as a workload of its own would run it
# Compiled for Java 11, so that the same jars run on every image; base images and the agent are pinned by SHA-256.
ARG JAVA=21
FROM docker.io/library/eclipse-temurin:11-jre-noble@sha256:53ff9958417a1a5e36eda6ae5e97e84b8350b266c151042d2af034d961f397ef AS java11
FROM docker.io/library/eclipse-temurin:17-jre-noble@sha256:8ee89ca3f9eb6a931a2b2e8b2901a4af3554383f8077558e3631d49bb58cff1d AS java17
FROM docker.io/library/eclipse-temurin:21-jre-noble@sha256:0eda5830ed7b669abc246a8dc520ea8a23cab06f9a8d4b6964109e2f2394e46f AS java21
FROM docker.io/library/eclipse-temurin:25-jre-noble@sha256:a98128a57de944a72bd31fbc79b898edd5a5864d3652071d5bbd2160ab7d7dce AS java25

FROM --platform=$BUILDPLATFORM docker.io/library/eclipse-temurin:21-jdk-noble@sha256:114cabb73033ca4ce7ceec3deb62396bc1b716282bae051ed3c1d82e38fcab2d AS build
WORKDIR /build
COPY src ./src
COPY agent ./agent
RUN javac --release 11 -d classes/app $(find src -name '*.java') \
 && javac --release 11 -d classes/agent $(find agent -name '*.java') \
 && printf 'Main-Class: fixture.App\n' >app.mf \
 && printf 'Premain-Class: fakeapm.Agent\n' >agent.mf \
 && jar --create --file app.jar --manifest app.mf -C classes/app . \
 && jar --create --file dd-java-agent.jar --manifest agent.mf -C classes/agent .
ADD --checksum=sha256:e43a270df53c6f423e0ab157be9c27ccc01d5dc3232e162fe54777573db2f149 https://repo1.maven.org/maven2/io/opentelemetry/javaagent/opentelemetry-javaagent/2.33.1/opentelemetry-javaagent-2.33.1.jar /build/opentelemetry-javaagent.jar
# Modes are set here and the jars copied without --chmod, which can give the directories COPY creates (/app, /agents)
# the jars' mode, so that no JVM can open them (GitHub's Ubuntu 26.04 runners).
RUN chmod 0644 app.jar dd-java-agent.jar opentelemetry-javaagent.jar

FROM java${JAVA}
COPY --from=build /build/app.jar /app/app.jar
COPY --from=build /build/dd-java-agent.jar /build/opentelemetry-javaagent.jar /agents/
WORKDIR /app
EXPOSE 8000
CMD ["java", "-jar", "/app/app.jar"]
