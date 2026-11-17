// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent;

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * The launcher's decisions, run by java/build.sh against the jar it just built: which agents count as another agent,
 * where they are looked for, the Java range, the switches that turn the kit off, the setup report's shape and the
 * layout of the jar. No test framework: the build image has a JDK and nothing else.
 */
public final class LauncherSelfTest {

  private static final List<String> failures = new ArrayList<>();
  private static int checks;

  public static void main(String[] args) throws Exception {
    File jar = new File(args[0]);
    File own = new File("/otel-auto-instrumentation-java-app/javaagent.jar");
    Settings built = settingsOf(jar);
    // The decisions below are checked against the default range, whatever range this build was given.
    Properties defaults = new Properties();
    defaults.putAll(propertiesOf(jar));
    defaults.setProperty("javaVersions", "17,21,25");
    Settings settings = new Settings(defaults);

    // The kit's own agent, where the Operator puts it, is not another agent.
    check("own agent", agents(env(own, map("JAVA_TOOL_OPTIONS", " -javaagent:" + own)), none()).isEmpty());

    // Another agent, wherever a JVM's options come from.
    expect(
        "Datadog in JAVA_TOOL_OPTIONS",
        env(own, map("JAVA_TOOL_OPTIONS", "-javaagent:/datadog-lib/dd-java-agent.jar -javaagent:" + own)),
        none(),
        "dd-java-agent.jar (-javaagent in JAVA_TOOL_OPTIONS)");
    expect(
        "an agent with arguments in JDK_JAVA_OPTIONS",
        env(own, map("JDK_JAVA_OPTIONS", "-Xmx1g -javaagent:/opt/newrelic/newrelic.jar=config=/x/y")),
        none(),
        "newrelic.jar (-javaagent in JDK_JAVA_OPTIONS)");
    expect(
        "JAVA_OPTS, quoted",
        env(own, map("JAVA_OPTS", "-Dx=1 \"-javaagent:/opt/my agent/elastic-apm-agent.jar\"")),
        none(),
        "elastic-apm-agent.jar (-javaagent in JAVA_OPTS)");
    expect(
        "an OpenTelemetry agent of its own on the command line",
        env(own, map()),
        Arrays.asList("java", "-Xmx512m", "-javaagent:/app/opentelemetry-javaagent.jar", "-jar", "app.jar"),
        "opentelemetry-javaagent.jar (-javaagent on the command line)");
    expect(
        "a native agent",
        env(own, map()),
        Arrays.asList("java", "-agentpath:/opt/dynatrace/oneagent/agent/lib64/liboneagentloader.so", "Main"),
        "liboneagentloader.so (-agentpath on the command line)");
    expect(
        "the same agent twice is named once",
        env(own, map("JAVA_OPTS", "-javaagent:/a/dd-java-agent.jar")),
        Arrays.asList("java", "-javaagent:/a/dd-java-agent.jar", "-cp", "x", "Main"),
        "dd-java-agent.jar (-javaagent in JAVA_OPTS)");

    // What is not another agent: a debugger, a profiler, options after the main class or jar, other options.
    check("jdwp", agents(env(own, map("JAVA_TOOL_OPTIONS", "-agentlib:jdwp=transport=dt_socket")), none()).isEmpty());
    check(
        "async-profiler",
        agents(env(own, map()), Arrays.asList("java", "-agentpath:/p/libasyncProfiler.so=start", "Main")).isEmpty());
    check(
        "an application argument after -jar",
        agents(env(own, map()), Arrays.asList("java", "-jar", "app.jar", "-javaagent:/x/y.jar")).isEmpty());
    check(
        "an application argument after the main class",
        agents(env(own, map()), Arrays.asList("java", "-cp", "-javaagent:odd", "Main", "-javaagent:/x/y.jar")).isEmpty());
    check("no options", agents(env(own, map("JAVA_TOOL_OPTIONS", "  ")), none()).isEmpty());

    // The name of an agent is cut, and so is the detail.
    String longName = repeat('a', 100) + ".jar";
    List<Detect.Finding> found = agents(env(own, map("JAVA_TOOL_OPTIONS", "-javaagent:/x/" + longName)), none());
    check("a long name is cut", found.size() == 1 && found.get(0).name.length() == Detect.MAX_NAME);
    StringBuilder many = new StringBuilder();
    for (int i = 0; i < 20; i++) {
      many.append(" -javaagent:/x/agent-").append(i).append(".jar");
    }
    String detail = Detect.describe(agents(env(own, map("JAVA_TOOL_OPTIONS", many.toString())), none()));
    check("the detail is cut to " + SetupReport.MAX_DETAIL, detail.length() == SetupReport.MAX_DETAIL && detail.endsWith("..."));

    // JAVA_TOOL_OPTIONS splits as the JVM splits it.
    equal("split", Arrays.asList("-a", "-b=c d", "-e"), Detect.split("  -a '-b=c d'\t-e "));

    // The Java range is the build's, and its wording is the row's.
    String wanted = System.getenv("JAVA_VERSIONS");
    Properties asked = new Properties();
    asked.setProperty("javaVersions", wanted == null || wanted.isEmpty() ? "17,21,25" : wanted);
    equal("the range is the build's", new Settings(asked).javaVersions(), built.javaVersions());
    Properties three = new Properties();
    three.setProperty("javaVersions", "25,17,21");
    equal(
        "the row's wording",
        "Impact AI traces Java 17, 21 and 25; this workload runs Java 11.0.29",
        new Settings(three).unsupported("11.0.29"));
    for (String[] version : new String[][] {{"1.8", "8"}, {"11", "11"}, {"17", "17"}, {"25", "25"}, {"26-ea", "0"}}) {
      equal("feature version of " + version[0], Integer.valueOf(version[1]), javaEnv(version[0], map()).javaFeatureVersion());
    }
    ImpactAgent.Outcome old = ImpactAgent.decide(settings, javaEnv("11", map()));
    check("Java 11 declines and reports", old != null && old.state.equals("failed") && old.reported && old.detail.contains("runs Java 11"));
    check("Java 21 proceeds", ImpactAgent.decide(settings, javaEnv("21", map())) == null);

    // The workload's switches turn the kit off without a report, before anything else is looked at.
    for (String[] off :
        new String[][] {
          {"OTEL_SDK_DISABLED", "true"}, {"OTEL_SDK_DISABLED", " TRUE "}, {"OTEL_JAVAAGENT_ENABLED", "false"}
        }) {
      ImpactAgent.Outcome outcome = ImpactAgent.decide(settings, javaEnv("11", map(off[0], off[1])));
      check(off[0] + "=" + off[1], outcome != null && outcome.state.equals("disabled") && !outcome.reported);
    }
    Properties disabled = new Properties();
    disabled.setProperty("java.specification.version", "21");
    disabled.setProperty("otel.javaagent.enabled", "false");
    check("-Dotel.javaagent.enabled=false", new Environment(map(), disabled, none(), own).disabled());
    check("OTEL_SDK_DISABLED=false is not off", !javaEnv("21", map("OTEL_SDK_DISABLED", "false")).disabled());

    // Another agent beside the kit: it stands down and says so.
    ImpactAgent.Outcome beside = ImpactAgent.decide(settings, javaEnv("21", map("JAVA_TOOL_OPTIONS", "-javaagent:/d/dd-java-agent.jar")));
    check("stand down", beside != null && beside.state.equals("pre-existing") && beside.detail.startsWith("dd-java-agent.jar"));

    // An OpenTelemetry pipeline of the workload's own, set up in its code: it stands down and names the settings.
    String gw = "http://gw:4318";
    ImpactAgent.Outcome ownSdk =
        ImpactAgent.decide(settings, javaEnv("21", map("IMPACT_OTLP_ENDPOINT", gw, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://theirs:4318")));
    check("own exporter endpoint", ownSdk != null && ownSdk.state.equals("pre-existing") && ownSdk.reported);
    equal("own exporter endpoint, named", "OTEL_EXPORTER_OTLP_ENDPOINT (set by the workload)", ownSdk == null ? null : ownSdk.detail);
    check("never its value", ownSdk != null && !ownSdk.detail.contains("theirs"));
    expectSettings(
        "every exporter setting, variables and properties",
        map(
            "IMPACT_OTLP_ENDPOINT", gw,
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://t:4318/v1/traces",
            "OTEL_EXPORTER_OTLP_HEADERS", "api-key=secret",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "x=y",
            "OTEL_EXPORTER_ZIPKIN_ENDPOINT", "http://z:9411",
            "OTEL_EXPORTER_JAEGER_ENDPOINT", " "),
        props("otel.exporter.otlp.endpoint", "http://p:4318", "otel.exporter.jaeger.timeout", "1000"),
        "otel.exporter.otlp.endpoint (set by the workload), OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (set by the workload), "
            + "OTEL_EXPORTER_OTLP_HEADERS (set by the workload), OTEL_EXPORTER_OTLP_TRACES_HEADERS (set by the workload), "
            + "OTEL_EXPORTER_ZIPKIN_ENDPOINT (set by the workload), otel.exporter.jaeger.timeout (set by the workload)");
    expectSettings("another trace exporter", map("OTEL_TRACES_EXPORTER", "zipkin"), props(), "OTEL_TRACES_EXPORTER (set by the workload)");
    expectSettings("another trace exporter, as a property", map(), props("otel.traces.exporter", "console"), "otel.traces.exporter (set by the workload)");
    expectSettings("what the Operator sets is not a signal", map("IMPACT_OTLP_ENDPOINT", gw, "OTEL_TRACES_EXPORTER", "otlp", "OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf", "OTEL_METRICS_EXPORTER", "none"), props(), "");
    expectSettings("outside the kit an endpoint is the kit's own", map("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318"), props(), "");
    ImpactAgent.Outcome both =
        ImpactAgent.decide(
            settings,
            javaEnv("21", map("IMPACT_OTLP_ENDPOINT", gw, "OTEL_EXPORTER_OTLP_ENDPOINT", "http://x", "JAVA_TOOL_OPTIONS", "-javaagent:/a/opentelemetry-javaagent.jar")));
    equal(
        "agents first",
        "opentelemetry-javaagent.jar (-javaagent in JAVA_TOOL_OPTIONS), OTEL_EXPORTER_OTLP_ENDPOINT (set by the workload)",
        both == null ? null : both.detail);

    // The setup report: the gateway inside the kit, the workload's endpoint outside it, nothing for a stood-down one.
    equal("kit endpoint", "http://gw.kit.svc:4318/v1/traces", SetupReport.tracesEndpoint(javaEnv("21", map("IMPACT_OTLP_ENDPOINT", "http://gw.kit.svc:4318/"))));
    equal("own endpoint", "http://collector:4318/v1/traces", SetupReport.tracesEndpoint(javaEnv("21", map("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318"))));
    check("no report of a stand-down outside the kit", SetupReport.report("pre-existing", "x", settings, javaEnv("21", map()), null) == null);
    Environment kit =
        javaEnv(
            "21",
            map(
                "OTEL_RESOURCE_ATTRIBUTES", "k8s.namespace.name=shop,k8s.deployment.name=orders,k8s.pod.name=orders-1",
                "IMPACT_OTLP_ENDPOINT", "http://gw:4318"));
    String json = SetupReport.build("pre-existing", "dd-java-agent.jar (\"x\")", "1.2.3", kit, null);
    for (String needle :
        new String[] {
          "\"name\":\"impact.setup\"",
          "{\"key\":\"impact.setup.state\",\"value\":{\"stringValue\":\"pre-existing\"}}",
          "{\"key\":\"impact.setup.detail\",\"value\":{\"stringValue\":\"dd-java-agent.jar (\\\"x\\\")\"}}",
          "{\"key\":\"impact.setup.package.version\",\"value\":{\"stringValue\":\"1.2.3\"}}",
          "{\"key\":\"impact.setup.java.version\",\"value\":{\"stringValue\":\"21\"}}",
          "{\"key\":\"impact.setup.frameworks\",\"value\":{\"arrayValue\":{\"values\":[]}}}",
          "{\"key\":\"impact.setup.untraced\",\"value\":{\"arrayValue\":{\"values\":[]}}}",
          "{\"key\":\"service.name\",\"value\":{\"stringValue\":\"orders\"}}",
          "{\"key\":\"telemetry.distro.name\",\"value\":{\"stringValue\":\"impact-instrumentation\"}}",
          "\"flags\":256",
        }) {
      check("report has " + needle, json.contains(needle));
    }
    check("no instrumentations on a stand-down", !json.contains("impact.setup.instrumentations"));
    String named = SetupReport.build("active", "", "1", javaEnv("21", map("OTEL_SERVICE_NAME", "checkout", "OTEL_RESOURCE_ATTRIBUTES", "k8s.deployment.name=orders")), "tomcat");
    check("OTEL_SERVICE_NAME names the service", named.contains("{\"key\":\"service.name\",\"value\":{\"stringValue\":\"checkout\"}}"));
    check("instrumentations on an active report", named.contains("{\"key\":\"impact.setup.instrumentations\",\"value\":{\"stringValue\":\"tomcat\"}}"));

    // A report is claimed once per container for the same content.
    File state = Files.createTempDirectory("impact-state").toFile();
    Environment claimer = javaEnv("21", map("IMPACT_STATE_DIR", state.getPath()));
    check("first claim", SetupReport.claim("active\0", claimer));
    check("second claim", !SetupReport.claim("active\0", claimer));
    check("other content", SetupReport.claim("failed\0x", claimer));
    check("an unwritable directory still reports", SetupReport.claim("x", javaEnv("21", map("IMPACT_STATE_DIR", "/nonexistent/dir"))));

    // The jar: the launcher, the two nested jars stored as they are, and the settings.
    try (ZipFile zip = new ZipFile(jar)) {
      for (String entry : new String[] {Payload.AGENT_ENTRY, Payload.EXTENSION_ENTRY}) {
        ZipEntry nested = zip.getEntry(entry);
        check(entry + " is stored", nested != null && nested.getMethod() == ZipEntry.STORED);
      }
      String manifest = new String(readAll(zip, "META-INF/MANIFEST.MF"), StandardCharsets.UTF_8);
      check("Premain-Class", manifest.contains("Premain-Class: ai.impact.instrumentation.javaagent.ImpactAgent"));
      check("retransform", manifest.contains("Can-Retransform-Classes: true"));
      check("no upstream class at the top", zip.getEntry(ImpactAgent.UPSTREAM_CLASS.replace('.', '/') + ".class") == null);
    }

    // Unpacking: the files are checked, written once and found again.
    File dir = Files.createTempDirectory("impact-payload").toFile();
    File copy = new File(dir, "javaagent.jar");
    Files.copy(jar.toPath(), copy.toPath());
    Payload.Files files = Payload.unpack(copy, built, javaEnv("21", map()));
    check("unpacked next to the jar", files.agent.getParentFile().getParentFile().equals(dir) && files.extension.isFile());
    long modified = files.agent.lastModified();
    Payload.Files again = Payload.unpack(copy, built, javaEnv("21", map()));
    check("found again", again.agent.equals(files.agent) && again.agent.lastModified() == modified);
    Properties wrong = new Properties();
    wrong.putAll(propertiesOf(jar));
    wrong.setProperty("agentSha256", repeat('0', 64));
    try {
      Payload.unpack(copy, new Settings(wrong), javaEnv("21", map("x", "y")));
      check("a wrong SHA-256 fails", false);
    } catch (java.io.IOException expected) {
      check("a wrong SHA-256 fails", expected.getMessage().contains("SHA-256"));
    }

    if (!failures.isEmpty()) {
      System.err.println("LauncherSelfTest: " + failures.size() + " of " + checks + " checks failed:");
      for (String failure : failures) {
        System.err.println("  " + failure);
      }
      System.exit(1);
    }
    System.out.println("LauncherSelfTest: " + checks + " checks passed");
  }

  private static Properties propertiesOf(File jar) throws Exception {
    Properties properties = new Properties();
    try (ZipFile zip = new ZipFile(jar)) {
      properties.load(new java.io.ByteArrayInputStream(readAll(zip, "ai/impact/instrumentation/javaagent/impact.properties")));
    }
    return properties;
  }

  private static Settings settingsOf(File jar) throws Exception {
    return new Settings(propertiesOf(jar));
  }

  private static byte[] readAll(ZipFile zip, String name) throws Exception {
    try (java.io.InputStream in = zip.getInputStream(zip.getEntry(name))) {
      java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
      byte[] buffer = new byte[8192];
      int n;
      while ((n = in.read(buffer)) > 0) {
        out.write(buffer, 0, n);
      }
      return out.toByteArray();
    }
  }

  private static void expect(String what, Environment env, List<String> command, String detail) {
    equal(what, detail, Detect.describe(agents(env, command)));
  }

  private static void expectSettings(String what, Map<String, String> variables, Properties properties, String detail) {
    StringBuilder found = new StringBuilder();
    for (Detect.Finding finding : Detect.exporterSettings(new Environment(variables, properties, none(), null))) {
      found.append(found.length() == 0 ? "" : ", ").append(finding.describe());
    }
    equal(what, detail, found.toString());
  }

  private static Properties props(String... pairs) {
    Properties out = new Properties();
    for (int i = 0; i + 1 < pairs.length; i += 2) {
      out.setProperty(pairs[i], pairs[i + 1]);
    }
    return out;
  }

  private static List<Detect.Finding> agents(Environment env, List<String> command) {
    return Detect.foreignAgents(new Environment(env.variables(), new Properties(), command, env.ownJar()));
  }

  private static Environment env(File own, Map<String, String> variables) {
    return new Environment(variables, new Properties(), none(), own);
  }

  private static Environment javaEnv(String version, Map<String, String> variables) {
    Properties properties = new Properties();
    properties.setProperty("java.specification.version", version);
    properties.setProperty("java.version", version);
    properties.setProperty("java.io.tmpdir", System.getProperty("java.io.tmpdir"));
    return new Environment(variables, properties, none(), new File("/otel-auto-instrumentation-java-app/javaagent.jar"));
  }

  private static List<String> none() {
    return Collections.emptyList();
  }

  private static Map<String, String> map(String... pairs) {
    Map<String, String> out = new HashMap<>();
    for (int i = 0; i + 1 < pairs.length; i += 2) {
      out.put(pairs[i], pairs[i + 1]);
    }
    return out;
  }

  private static String repeat(char c, int n) {
    char[] chars = new char[n];
    Arrays.fill(chars, c);
    return new String(chars);
  }

  private static void check(String what, boolean ok) {
    checks++;
    if (!ok) {
      failures.add(what);
    }
  }

  private static void equal(String what, Object expected, Object actual) {
    checks++;
    if (!expected.equals(actual)) {
      failures.add(what + ": expected <" + expected + ">, got <" + actual + ">");
    }
  }
}
