#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0

# Build the kit's javaagent.jar from the upstream agent and this directory's sources, then run the self tests:
#
#   java/build.sh <dependencies> <out>      (JAVA_HOME or javac on PATH: a JDK of 21 or later)
#
# <dependencies> holds opentelemetry-javaagent.jar (the upstream agent, checked by its SHA-256 before anything uses it)
# and compile/*.jar (the OpenTelemetry API and SDK jars the extension compiles against; the agent brings its own copy at
# run time). The Dockerfile fetches both by SHA-256. <out> gets javaagent.jar and licenses/.
#
# javaagent.jar is a small launcher (compiled for Java 8, so that any JVM can load it and report) that carries the
# upstream agent and the kit's extension as nested, uncompressed jars, and impact.properties with the version, the Java
# versions the image is built for (JAVA_VERSIONS, default 17,21,25), the SHA-256 of both nested jars and the
# instrumentations of scope.txt. The build is reproducible: every entry has the same time.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
deps="$(cd "$1" && pwd)"
mkdir -p "$2"
out="$(cd "$2" && pwd)"
VERSION="${VERSION:-0.0.0-dev}"
JAVA_VERSIONS="${JAVA_VERSIONS:-17,21,25}"
AGENT_SHA256="${AGENT_SHA256:?AGENT_SHA256 is required: the SHA-256 of the upstream agent}"
DATE="2026-01-01T00:00:00Z"
bin="${JAVA_HOME:+$JAVA_HOME/bin/}"

if ! [[ "$JAVA_VERSIONS" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
  echo "JAVA_VERSIONS=$JAVA_VERSIONS: expected feature releases separated by commas, for example 17,21,25" >&2
  exit 2
fi
agent="$deps/opentelemetry-javaagent.jar"
echo "$AGENT_SHA256  $agent" | sha256sum -c --quiet -

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
compile_cp="$(find "$deps/compile" -name '*.jar' | sort | paste -sd: -)"
instrumentations="$(sed -e 's/#.*//' -e 's/[[:space:]]//g' "$here/scope.txt" | sed '/^$/d' | paste -sd, -)"
javac=("${bin}javac" --release 8 -encoding UTF-8 -Xlint:all,-options -Werror)

settings() { # settings <file> <extension sha256 or empty>
  {
    echo "version=$VERSION"
    echo "javaVersions=$JAVA_VERSIONS"
    echo "agentSha256=$AGENT_SHA256"
    [ -z "$2" ] || echo "extensionSha256=$2"
    echo "instrumentations=$instrumentations"
  } >"$1"
}

# The extension: loaded by the upstream agent from its own class loader.
mkdir -p "$work/extension/ai/impact/instrumentation/javaagent"
"${javac[@]}" -cp "$compile_cp" -d "$work/extension" $(find "$here/extension" -name '*.java' | sort)
cp -R "$here/extension/META-INF" "$work/extension/"
settings "$work/extension/ai/impact/instrumentation/javaagent/impact.properties" ""
"${bin}jar" --create --file "$work/impact-extension.jar" --date="$DATE" -C "$work/extension" .
extension_sha="$(sha256sum "$work/impact-extension.jar" | cut -c1-64)"

# The launcher, with the two jars nested and stored without compression (they are compressed already).
mkdir -p "$work/launcher/impact"
"${javac[@]}" -d "$work/launcher" $(find "$here/launcher" -name '*.java' | sort)
settings "$work/launcher/ai/impact/instrumentation/javaagent/impact.properties" "$extension_sha"
cp "$agent" "$work/launcher/impact/opentelemetry-javaagent.jar"
cp "$work/impact-extension.jar" "$work/launcher/impact/impact-extension.jar"
cat >"$work/manifest.txt" <<EOF
Premain-Class: ai.impact.instrumentation.javaagent.ImpactAgent
Agent-Class: ai.impact.instrumentation.javaagent.ImpactAgent
Can-Redefine-Classes: true
Can-Retransform-Classes: true
Implementation-Title: impact.ai Java auto-instrumentation
Implementation-Version: $VERSION
EOF
"${bin}jar" --create --file "$out/javaagent.jar" --manifest "$work/manifest.txt" --no-compress --date="$DATE" \
  -C "$work/launcher" .

# The self tests: the launcher's decisions and the extension's filters, without a container.
mkdir -p "$work/tests"
"${javac[@]}" -cp "$work/launcher:$work/extension:$compile_cp" -d "$work/tests" $(find "$here/test-src" -name '*.java' | sort)
"${bin}java" -cp "$work/tests:$work/launcher" ai.impact.instrumentation.javaagent.LauncherSelfTest "$out/javaagent.jar"
"${bin}java" -cp "$work/tests:$work/extension:$compile_cp" ai.impact.instrumentation.javaagent.extension.ExtensionSelfTest

# The licenses of what the image bundles, as the upstream agent ships them.
rm -rf "$out/licenses"
mkdir -p "$out/licenses/opentelemetry-javaagent"
(cd "$out/licenses/opentelemetry-javaagent" && "${bin}jar" --extract --file "$agent" META-INF/licenses \
  inst/META-INF/FastDoubleParser-LICENSE inst/META-INF/FastDoubleParser-ThirdParty-LICENSE inst/META-INF/Schubfach-LICENSE)
echo "built $out/javaagent.jar: agent $AGENT_SHA256, extension $extension_sha, Java $JAVA_VERSIONS"
