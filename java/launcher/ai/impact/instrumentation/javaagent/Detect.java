// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent;

import java.io.File;
import java.io.IOException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * Finds what shows that a workload owns its tracing, so that the kit stands down beside it: another agent in the JVM's
 * options (an APM vendor's, or an OpenTelemetry agent of its own), or exporter settings of its own (an OpenTelemetry SDK
 * the application sets up in its code).
 *
 * <p>Agents are looked for where a JVM's options come from: {@code JAVA_TOOL_OPTIONS} (read by the JVM, and where the
 * Operator puts the kit's own agent), {@code JDK_JAVA_OPTIONS} (read by the launcher), {@code JAVA_OPTS} (what start
 * scripts put on the command line) and the command line itself, up to the main class or jar. Exporter settings are the
 * ones the Python and Node.js images stand down on, as variables or system properties. It reads names only: a finding is
 * the file name of the agent and where it was found, or the name of a setting, never a value or an argument.
 */
final class Detect {

  static final int MAX_NAME = 64;

  /** Options of the {@code java} launcher that take the next argument as their value. */
  private static final Set<String> TAKES_VALUE =
      new HashSet<>(
          Arrays.asList(
              "-cp",
              "-classpath",
              "--class-path",
              "-p",
              "--module-path",
              "--upgrade-module-path",
              "--add-modules",
              "--limit-modules",
              "--add-exports",
              "--add-opens",
              "--add-reads",
              "--patch-module",
              "--enable-native-access",
              "--source"));

  /**
   * Native agents that do not trace: a debugger and profilers. A workload that runs one of them is still covered. Matched
   * against the library's name, without "lib" and the file extension.
   */
  private static final List<String> NOT_TRACERS = Arrays.asList("jdwp", "asyncprofiler", "yjpagent", "jprofilerti");

  /**
   * The exporter settings a workload sets when it runs its own OpenTelemetry pipeline, as system properties (the
   * variables are their upper-case forms). OTEL_EXPORTER_OTLP_PROTOCOL and OTEL_{TRACES,METRICS,LOGS}_EXPORTER=otlp are
   * set by the Operator itself and are not signals.
   */
  private static final List<String> EXPORTER_SETTINGS =
      Arrays.asList(
          "otel.exporter.otlp.endpoint",
          "otel.exporter.otlp.traces.endpoint",
          "otel.exporter.otlp.headers",
          "otel.exporter.otlp.traces.headers");

  private static final List<String> EXPORTER_PREFIXES = Arrays.asList("otel.exporter.zipkin.", "otel.exporter.jaeger.");

  static final String KIT_ENDPOINT = "IMPACT_OTLP_ENDPOINT";

  private Detect() {}

  /** One thing found: an agent's file name and where it was found, or the name of a setting of the workload's. */
  static final class Finding {
    final String option;
    final String name;
    final String where;

    Finding(String option, String name, String where) {
      this.option = option;
      this.name = name;
      this.where = where;
    }

    static Finding setting(String name) {
      return new Finding(null, name, null);
    }

    String describe() {
      return option == null ? name + " (set by the workload)" : name + " (" + option + " " + where + ")";
    }

    @Override
    public String toString() {
      return describe();
    }
  }

  static List<Finding> foreignAgents(Environment env) {
    Map<String, Finding> found = new LinkedHashMap<>();
    scan(split(env.variable("JAVA_TOOL_OPTIONS")), "in JAVA_TOOL_OPTIONS", env, found, false);
    scan(split(env.variable("JDK_JAVA_OPTIONS")), "in JDK_JAVA_OPTIONS", env, found, false);
    scan(split(env.variable("JAVA_OPTS")), "in JAVA_OPTS", env, found, false);
    List<String> command = env.commandLine();
    if (!command.isEmpty()) {
      scan(command.subList(1, command.size()), "on the command line", env, found, true);
    }
    return new ArrayList<>(found.values());
  }

  /** Everything that shows the workload owns its tracing: other agents first, then its exporter settings. */
  static List<Finding> foreign(Environment env) {
    List<Finding> found = foreignAgents(env);
    found.addAll(exporterSettings(env));
    return found;
  }

  /**
   * The workload's own exporter settings, by name. A trace exporter other than OTLP is the workload's anywhere. Inside
   * the kit, whose endpoint is IMPACT_OTLP_ENDPOINT, so is any OTLP endpoint or header setting and any Zipkin or Jaeger
   * one. The Python and Node.js images count these only when the application also ships the OpenTelemetry SDK; a JVM's
   * class path cannot be read before the application starts (start scripts, fat jars), so here the setting alone is
   * enough, and the error is on the side of standing down.
   */
  static List<Finding> exporterSettings(Environment env) {
    List<Finding> found = new ArrayList<>();
    String traces = env.setting("otel.traces.exporter");
    if (!traces.isEmpty() && !traces.equals("otlp")) {
      found.add(Finding.setting(nameOf(env, "otel.traces.exporter")));
    }
    if (env.variable(KIT_ENDPOINT).trim().isEmpty()) {
      return found;
    }
    for (String setting : EXPORTER_SETTINGS) {
      if (!env.setting(setting).isEmpty()) {
        found.add(Finding.setting(nameOf(env, setting)));
      }
    }
    Set<String> others = new TreeSet<>();
    for (String name : env.propertyNames()) {
      if (startsWithAny(name, EXPORTER_PREFIXES) && !env.property(name).trim().isEmpty()) {
        others.add(name);
      }
    }
    for (Map.Entry<String, String> variable : env.variables().entrySet()) {
      String name = variable.getKey();
      if (startsWithAny(name.toLowerCase(Locale.ROOT).replace('_', '.'), EXPORTER_PREFIXES)
          && variable.getValue() != null
          && !variable.getValue().trim().isEmpty()) {
        others.add(name);
      }
    }
    for (String name : others) {
      found.add(Finding.setting(name.length() > MAX_NAME ? name.substring(0, MAX_NAME) : name));
    }
    return found;
  }

  /** The system property when it is set, else the variable the agent reads in its place. */
  private static String nameOf(Environment env, String property) {
    return env.property(property).trim().isEmpty()
        ? property.toUpperCase(Locale.ROOT).replace('.', '_').replace('-', '_')
        : property;
  }

  private static boolean startsWithAny(String name, List<String> prefixes) {
    for (String prefix : prefixes) {
      if (name.startsWith(prefix)) {
        return true;
      }
    }
    return false;
  }

  /** "dd-java-agent.jar (-javaagent in JAVA_TOOL_OPTIONS), ...", cut to the setup report's limit. */
  static String describe(List<Finding> findings) {
    StringBuilder text = new StringBuilder();
    for (Finding finding : findings) {
      if (text.length() > 0) {
        text.append(", ");
      }
      text.append(finding.describe());
    }
    return SetupReport.cut(text.toString());
  }

  private static void scan(
      List<String> arguments, String where, Environment env, Map<String, Finding> found, boolean commandLine) {
    for (int i = 0; i < arguments.size(); i++) {
      String argument = arguments.get(i);
      if (commandLine) {
        if (argument.equals("-jar") || argument.equals("-m") || argument.equals("--module")) {
          return; // what follows the jar or the module belongs to the application
        }
        if (TAKES_VALUE.contains(argument)) {
          i++;
          continue;
        }
        if (!argument.startsWith("-")) {
          return; // the main class
        }
      }
      Finding finding = agentOf(argument, where, env);
      if (finding != null && !found.containsKey(finding.option + finding.name)) {
        found.put(finding.option + finding.name, finding);
      }
    }
  }

  /** The agent an option loads, or null when it loads none, loads the kit's own or loads a debugger or profiler. */
  static Finding agentOf(String argument, String where, Environment env) {
    String option;
    String target;
    if (argument.startsWith("-javaagent:")) {
      option = "-javaagent";
      target = argument.substring("-javaagent:".length());
    } else if (argument.startsWith("-agentpath:")) {
      option = "-agentpath";
      target = argument.substring("-agentpath:".length());
    } else if (argument.startsWith("-agentlib:")) {
      option = "-agentlib";
      target = argument.substring("-agentlib:".length());
    } else {
      return null;
    }
    int equals = target.indexOf('=');
    if (equals >= 0) {
      target = target.substring(0, equals);
    }
    target = target.trim();
    if (target.isEmpty()) {
      return null;
    }
    if (option.equals("-javaagent") && isOwn(target, env.ownJar())) {
      return null;
    }
    String name = new File(target).getName();
    if (!option.equals("-javaagent") && isNotATracer(name)) {
      return null;
    }
    if (name.length() > MAX_NAME) {
      name = name.substring(0, MAX_NAME);
    }
    return new Finding(option, name, where);
  }

  private static boolean isNotATracer(String file) {
    String name = file.toLowerCase(Locale.ROOT);
    if (name.startsWith("lib")) {
      name = name.substring(3);
    }
    int dot = name.indexOf('.');
    if (dot >= 0) {
      name = name.substring(0, dot);
    }
    return NOT_TRACERS.contains(name);
  }

  static boolean isOwn(String path, File own) {
    if (own == null) {
      return false;
    }
    File candidate = new File(path);
    if (candidate.getAbsolutePath().equals(own.getAbsolutePath())) {
      return true;
    }
    try {
      return candidate.getCanonicalPath().equals(own.getCanonicalPath());
    } catch (IOException | SecurityException e) {
      return false;
    }
  }

  /**
   * Options as the JVM splits {@code JAVA_TOOL_OPTIONS}: at white space, with single or double quotes keeping a value
   * together.
   */
  static List<String> split(String options) {
    List<String> out = new ArrayList<>();
    StringBuilder current = new StringBuilder();
    char quote = 0;
    boolean inToken = false;
    for (int i = 0; i < options.length(); i++) {
      char c = options.charAt(i);
      if (quote != 0) {
        if (c == quote) {
          quote = 0;
        } else {
          current.append(c);
        }
      } else if (c == '"' || c == '\'') {
        quote = c;
        inToken = true;
      } else if (Character.isWhitespace(c)) {
        if (inToken) {
          out.add(current.toString());
          current.setLength(0);
          inToken = false;
        }
      } else {
        current.append(c);
        inToken = true;
      }
    }
    if (inToken) {
      out.add(current.toString());
    }
    return out;
  }
}
