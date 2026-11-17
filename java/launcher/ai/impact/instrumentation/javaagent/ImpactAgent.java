// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent;

import java.lang.instrument.Instrumentation;
import java.lang.reflect.Method;
import java.net.URL;
import java.util.List;
import java.util.jar.JarFile;

/**
 * The Premain-Class of the kit's Java image: the file the OpenTelemetry Operator copies into the pod and names in
 * {@code JAVA_TOOL_OPTIONS=-javaagent:/otel-auto-instrumentation-java-<container>/javaagent.jar}.
 *
 * <p>It decides before anything of OpenTelemetry is loaded: it stands down when the workload already runs another Java
 * agent or sets up an exporter of its own, and declines on a Java outside the range the image is built for. Only then does it unpack the upstream agent
 * it carries, put it on the class path and start it with the kit's extension. The upstream agent is nested, not merged,
 * so that a workload's own OpenTelemetry agent, which has the same Premain-Class, never finds the kit's copy first.
 *
 * <p>Nothing here may stop the application: every failure is caught and reported in the {@code impact.setup} span, and
 * the application starts as it would without the kit. The class is compiled for Java 8 so that an old JVM can report.
 */
public final class ImpactAgent {

  static final String UPSTREAM_CLASS = "io.opentelemetry.javaagent.OpenTelemetryAgent";

  private ImpactAgent() {}

  public static void premain(String agentArgs, Instrumentation inst) {
    start(agentArgs, inst, Environment.current());
  }

  /** Attaching to a running JVM is not supported: the kit starts with the process or not at all. */
  public static void agentmain(String agentArgs, Instrumentation inst) {}

  static Outcome start(String agentArgs, Instrumentation inst, Environment env) {
    Settings settings = null;
    try {
      settings = Settings.load();
      Outcome decision = decide(settings, env);
      if (decision != null) {
        if (decision.reported) {
          SetupReport.report(decision.state, decision.detail, settings, env, null);
        }
        return decision;
      }
      Payload.Files files = Payload.unpack(env.ownJar(), settings, env);
      String extensions = files.extension.getAbsolutePath();
      System.setProperty("otel.javaagent.extensions", extensions);
      inst.appendToSystemClassLoaderSearch(new JarFile(files.agent, false));
      ClassLoader system = ClassLoader.getSystemClassLoader();
      URL found = system.getResource(UPSTREAM_CLASS.replace('.', '/') + ".class");
      String unpacked = files.agent.getParentFile().getName() + "/" + files.agent.getName();
      if (found == null || !found.toString().contains(unpacked)) {
        // The application's class path already holds an agent's classes (an uber jar): starting would load those.
        throw new IllegalStateException("the class path holds another copy of the OpenTelemetry agent");
      }
      Class<?> upstream = Class.forName(UPSTREAM_CLASS, true, system);
      Method premain = upstream.getMethod("premain", String.class, Instrumentation.class);
      premain.invoke(null, agentArgs, inst);
      Outcome active = new Outcome("active", "", true);
      SetupReport.report(active.state, active.detail, settings, env, settings.instrumentations());
      return active;
    } catch (Throwable error) {
      String detail = failure(error);
      try {
        System.clearProperty("otel.javaagent.extensions");
        SetupReport.report("failed", detail, settings, env, null);
      } catch (Throwable ignored) {
        // reporting is best effort and must never reach the application
      }
      return new Outcome("failed", detail, true);
    }
  }

  /** Why the agent does not start, or null when it should. */
  static Outcome decide(Settings settings, Environment env) {
    if (env.disabled()) {
      return new Outcome("disabled", "", false);
    }
    int feature = env.javaFeatureVersion();
    if (!settings.javaVersions().contains(feature)) {
      return new Outcome("failed", settings.unsupported(env.javaVersion()), true);
    }
    List<Detect.Finding> findings = Detect.foreign(env);
    if (!findings.isEmpty()) {
      return new Outcome("pre-existing", Detect.describe(findings), true);
    }
    return null;
  }

  static String failure(Throwable error) {
    Throwable cause = error;
    while (cause.getCause() != null && cause.getCause() != cause) {
      cause = cause.getCause();
    }
    String message = cause.getMessage();
    String text = cause.getClass().getSimpleName() + (message == null ? "" : ": " + message);
    int newline = text.indexOf('\n');
    if (newline >= 0) {
      text = text.substring(0, newline);
    }
    return SetupReport.cut(text);
  }

  /** What the launcher did: the {@code impact.setup} state, its detail and whether it was reported. */
  static final class Outcome {
    final String state;
    final String detail;
    final boolean reported;

    Outcome(String state, String detail, boolean reported) {
      this.state = state;
      this.detail = detail;
      this.reported = reported;
    }

    @Override
    public String toString() {
      return state + (detail.isEmpty() ? "" : ": " + detail);
    }
  }
}
