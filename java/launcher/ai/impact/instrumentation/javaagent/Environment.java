// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.Set;

/**
 * What the launcher reads of the process: environment variables, system properties, the command line, the Java version
 * and its own jar. One object, so that the self test can give it any process it likes.
 */
final class Environment {

  private final Map<String, String> variables;
  private final Properties properties;
  private final List<String> commandLine;
  private final File ownJar;

  Environment(Map<String, String> variables, Properties properties, List<String> commandLine, File ownJar) {
    this.variables = variables;
    this.properties = properties;
    this.commandLine = commandLine;
    this.ownJar = ownJar;
  }

  static Environment current() {
    return new Environment(System.getenv(), System.getProperties(), readCommandLine(), locateOwnJar());
  }

  String variable(String name) {
    String value = variables.get(name);
    return value == null ? "" : value;
  }

  boolean hasVariable(String name) {
    return variables.containsKey(name);
  }

  Map<String, String> variables() {
    return variables;
  }

  /** The names of the system properties: what the JVM's -D options set, at premain. */
  Set<String> propertyNames() {
    return properties.stringPropertyNames();
  }

  String property(String name) {
    String value = properties.getProperty(name);
    return value == null ? "" : value;
  }

  /** The JVM's arguments as the process was started: what a start script put there from JAVA_OPTS, for example. */
  List<String> commandLine() {
    return commandLine;
  }

  File ownJar() {
    return ownJar;
  }

  String javaVersion() {
    String version = property("java.version");
    return version.isEmpty() ? property("java.specification.version") : version;
  }

  /** 8 for "1.8", 17 for "17": the feature release of the running JVM; 0 when it cannot be read. */
  int javaFeatureVersion() {
    String spec = property("java.specification.version");
    if (spec.startsWith("1.")) {
      spec = spec.substring(2);
    }
    int dot = spec.indexOf('.');
    if (dot >= 0) {
      spec = spec.substring(0, dot);
    }
    try {
      return Integer.parseInt(spec.trim());
    } catch (NumberFormatException e) {
      return 0;
    }
  }

  /**
   * The workload switched OpenTelemetry off: the agent's own switches, as a system property or a variable. The kit does
   * nothing and reports nothing then, as the Python and Node.js images do.
   */
  boolean disabled() {
    return isTrue(setting("otel.sdk.disabled")) || "false".equals(setting("otel.javaagent.enabled"));
  }

  /** An OpenTelemetry setting as the agent reads it: the system property first, then the variable. */
  String setting(String property) {
    String value = property(property);
    if (value.isEmpty()) {
      value = variable(property.toUpperCase(Locale.ROOT).replace('.', '_').replace('-', '_'));
    }
    return value.trim().toLowerCase(Locale.ROOT);
  }

  private static boolean isTrue(String value) {
    return "true".equals(value);
  }

  /** /proc/self/cmdline, split at its NUL bytes; empty where there is no such file. */
  static List<String> readCommandLine() {
    File file = new File("/proc/self/cmdline");
    if (!file.isFile()) {
      return Collections.emptyList();
    }
    try (InputStream in = new FileInputStream(file)) {
      ByteArrayOutputStream out = new ByteArrayOutputStream();
      byte[] buffer = new byte[8192];
      int n;
      while ((n = in.read(buffer)) > 0 && out.size() < 1 << 20) {
        out.write(buffer, 0, n);
      }
      return splitNul(out.toByteArray());
    } catch (IOException | RuntimeException e) {
      return Collections.emptyList();
    }
  }

  static List<String> splitNul(byte[] data) {
    List<String> parts = new ArrayList<>();
    int start = 0;
    for (int i = 0; i <= data.length; i++) {
      if (i == data.length || data[i] == 0) {
        if (i > start) {
          parts.add(new String(data, start, i - start, StandardCharsets.UTF_8));
        }
        start = i + 1;
      }
    }
    return parts;
  }

  private static File locateOwnJar() {
    try {
      return new File(ImpactAgent.class.getProtectionDomain().getCodeSource().getLocation().toURI());
    } catch (Exception | LinkageError e) {
      return null;
    }
  }
}
