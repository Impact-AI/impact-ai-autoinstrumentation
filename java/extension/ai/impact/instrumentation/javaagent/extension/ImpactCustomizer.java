// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent.extension;

import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.sdk.autoconfigure.spi.AutoConfigurationCustomizer;
import io.opentelemetry.sdk.autoconfigure.spi.AutoConfigurationCustomizerProvider;
import io.opentelemetry.sdk.autoconfigure.spi.ConfigProperties;
import io.opentelemetry.sdk.resources.Resource;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.TreeSet;

/**
 * The kit's configuration of the upstream agent, carried in the image instead of OTEL_* variables in the pod.
 *
 * <p>The scope is cut to what the Python image traces: request roots (HTTP servers), HTTP clients, context propagation
 * and the GenAI modules. Every other instrumentation is off, the agent's metrics are off, the logging appenders are off
 * so that no application log line leaves the pod, health probes make no spans, and only GenAI events are exported as
 * logs. Inside the kit everything goes to the kit's gateway (IMPACT_OTLP_ENDPOINT). These are overrides: a variable in
 * the pod does not widen the scope. What the workload may set is a default: the content mode (on unless the workload
 * turns it off) and the service name (the workload's name unless the workload names its service).
 */
public final class ImpactCustomizer implements AutoConfigurationCustomizerProvider {

  static final String KIT_ENDPOINT = "IMPACT_OTLP_ENDPOINT";
  static final String SETTINGS = "/ai/impact/instrumentation/javaagent/impact.properties";
  private static final String[] WORKLOAD_NAME_KEYS = {
    "k8s.deployment.name",
    "k8s.statefulset.name",
    "k8s.daemonset.name",
    "k8s.cronjob.name",
    "k8s.job.name",
    "k8s.pod.name",
  };

  /**
   * The options of the scope's instrumentations that end in .enabled and are not experimental, normalized: netty's and
   * reactor-netty's connection telemetry and netty's TLS telemetry (agent 2.33.1).
   */
  private static final Set<String> OPTIONS = new HashSet<>(Arrays.asList("connection.telemetry", "ssl.telemetry"));

  private static final String PREFIX = "otel.instrumentation.";
  private static final String SUFFIX = ".enabled";

  private final Map<String, String> environment;
  private final Properties system;
  private final Properties settings;

  public ImpactCustomizer() {
    this(System.getenv(), System.getProperties(), loadSettings());
  }

  ImpactCustomizer(Map<String, String> environment, Properties settings) {
    this(environment, new Properties(), settings);
  }

  ImpactCustomizer(Map<String, String> environment, Properties system, Properties settings) {
    this.environment = environment;
    this.system = system;
    this.settings = settings;
  }

  @Override
  public void customize(AutoConfigurationCustomizer customizer) {
    customizer.addPropertiesSupplier(this::defaults);
    customizer.addPropertiesCustomizer(this::overrides);
    customizer.addSamplerCustomizer((sampler, config) -> new ProbeSampler(sampler));
    customizer.addLogRecordExporterCustomizer((exporter, config) -> new GenAiEventsOnly(exporter));
    customizer.addResourceCustomizer((resource, config) -> resource.merge(distro()));
  }

  /** Runs after the agent's own customizers, so that the overrides are the last word. */
  @Override
  public int order() {
    return Integer.MAX_VALUE;
  }

  /** What the workload may change. */
  Map<String, String> defaults() {
    Map<String, String> out = new HashMap<>();
    out.put("otel.instrumentation.genai.capture-message-content", "true");
    String service = workloadName();
    if (service != null) {
      out.put("otel.service.name", service);
    }
    return out;
  }

  /** What the kit decides whatever the pod says. */
  Map<String, String> overrides(ConfigProperties config) {
    Map<String, String> out = new LinkedHashMap<>();
    out.put("otel.instrumentation.common.default-enabled", "false");
    for (String name : instrumentations()) {
      out.put("otel.instrumentation." + name + ".enabled", "true");
    }
    // default-enabled=false does not turn off what the workload turns on by name: that setting is cleared here, so that
    // the module falls back to off. Cleared, not set to false: the agent takes the first of a module's names that has a
    // value, and a name outside the scope can come before one of the scope (every AWS SDK module is `aws-sdk` first, so
    // aws-sdk=false would turn Bedrock off too, while aws-sdk=true turns SQS and SNS on).
    for (String name : widened(config)) {
      out.put(PREFIX + name + SUFFIX, "");
    }
    out.put("otel.traces.exporter", "otlp");
    out.put("otel.metrics.exporter", "none");
    out.put("otel.logs.exporter", "otlp");
    out.put("otel.instrumentation.runtime-telemetry.enabled", "false");
    String kit = value(KIT_ENDPOINT);
    if (!kit.isEmpty()) {
      while (kit.endsWith("/")) {
        kit = kit.substring(0, kit.length() - 1);
      }
      out.put("otel.exporter.otlp.protocol", "http/protobuf");
      out.put("otel.exporter.otlp.endpoint", kit);
      out.put("otel.exporter.otlp.headers", "");
      for (String signal : new String[] {"traces", "logs"}) {
        out.put("otel.exporter.otlp." + signal + ".protocol", "http/protobuf");
        out.put("otel.exporter.otlp." + signal + ".endpoint", kit + "/v1/" + signal);
        out.put("otel.exporter.otlp." + signal + ".headers", "");
      }
    }
    return out;
  }

  String[] instrumentations() {
    String list = settings.getProperty("instrumentations", "");
    return list.trim().isEmpty() ? new String[0] : list.trim().split("\\s*,\\s*");
  }

  /**
   * The instrumentations outside the scope that the workload turns on by name, in an environment variable, a system
   * property or the agent's configuration file, by the agent's normalized names (lower case, dots for - and _).
   */
  Set<String> widened(ConfigProperties config) {
    Map<String, String> asked = new HashMap<>();
    for (Map.Entry<String, String> e : environment.entrySet()) {
      if (e.getKey().startsWith("OTEL_INSTRUMENTATION_")) {
        asked.put(e.getKey(), e.getValue());
      }
    }
    for (String key : system.stringPropertyNames()) {
      asked.put(key, system.getProperty(key));
    }
    String file = config == null ? null : config.getString("otel.javaagent.configuration-file");
    if (file == null || file.trim().isEmpty()) {
      file = system.getProperty("otel.javaagent.configuration-file", value("OTEL_JAVAAGENT_CONFIGURATION_FILE"));
    }
    if (!file.trim().isEmpty()) {
      Properties fromFile = new Properties();
      try (InputStream in = new FileInputStream(file.trim())) {
        fromFile.load(in);
      } catch (IOException | RuntimeException e) {
        // the agent reports a file it cannot read; nothing in it widens the scope
      }
      for (String key : fromFile.stringPropertyNames()) {
        asked.put(key, fromFile.getProperty(key));
      }
    }
    Set<String> scope = new HashSet<>();
    for (String name : instrumentations()) {
      scope.add(normalize(name));
    }
    Set<String> out = new TreeSet<>();
    for (Map.Entry<String, String> e : asked.entrySet()) {
      String key = normalize(e.getKey());
      if (!key.startsWith(PREFIX) || !key.endsWith(SUFFIX) || key.length() <= PREFIX.length() + SUFFIX.length()) {
        continue;
      }
      String name = key.substring(PREFIX.length(), key.length() - SUFFIX.length());
      if (!"true".equalsIgnoreCase(e.getValue() == null ? "" : e.getValue().trim())
          || name.startsWith("common.")
          || inScope(scope, name)) {
        continue;
      }
      out.add(name);
    }
    return out;
  }

  /**
   * A name of the scope, or an option of one (otel.instrumentation.netty.connection-telemetry.enabled). A name that only
   * starts with a scope name is another instrumentation (reactor-kafka is not an option of reactor, tomcat-jdbc not one
   * of tomcat), so an option is a known one or an experimental one. Turning off a name that is neither is harmless: a
   * module of the scope stays on through its own name, because the agent turns a module on when any of its names is.
   */
  static boolean inScope(Set<String> scope, String name) {
    if (scope.contains(name)) {
      return true;
    }
    for (String s : scope) {
      if (name.startsWith(s + ".")) {
        String option = name.substring(s.length() + 1);
        if (OPTIONS.contains(option) || option.startsWith("experimental.")) {
          return true;
        }
      }
    }
    return false;
  }

  /** The key as the agent reads it: lower case, with dots for dashes and underscores. */
  static String normalize(String key) {
    return key.toLowerCase(Locale.ROOT).replace('-', '.').replace('_', '.');
  }

  /** The workload's name from the Operator's OTEL_RESOURCE_ATTRIBUTES, when nothing names the service. */
  String workloadName() {
    if (!value("OTEL_SERVICE_NAME").isEmpty()) {
      return null;
    }
    Map<String, String> attributes = new HashMap<>();
    for (String pair : value("OTEL_RESOURCE_ATTRIBUTES").split(",")) {
      int at = pair.indexOf('=');
      if (at > 0) {
        attributes.put(pair.substring(0, at).trim(), pair.substring(at + 1).trim());
      }
    }
    if (attributes.containsKey("service.name")) {
      return null;
    }
    for (String key : WORKLOAD_NAME_KEYS) {
      String name = attributes.get(key);
      if (name != null && !name.isEmpty()) {
        return name;
      }
    }
    return null;
  }

  Resource distro() {
    return Resource.create(
        Attributes.builder()
            .put("telemetry.distro.name", "impact-instrumentation")
            .put("telemetry.distro.version", settings.getProperty("version", "unknown"))
            .build());
  }

  private String value(String name) {
    String value = environment.get(name);
    return value == null ? "" : value.trim();
  }

  static Properties loadSettings() {
    Properties properties = new Properties();
    try (InputStream in = ImpactCustomizer.class.getResourceAsStream(SETTINGS)) {
      if (in != null) {
        properties.load(in);
      }
    } catch (IOException e) {
      // the defaults of an empty file: nothing beyond the agent's own scope cut
    }
    return properties;
  }
}
