// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent.extension;

import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.logs.data.LogRecordData;
import io.opentelemetry.sdk.logs.export.LogRecordExporter;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import io.opentelemetry.sdk.trace.samplers.SamplingDecision;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;

/** The extension's configuration and filters, run by java/build.sh with the SDK jars it compiled against. */
public final class ExtensionSelfTest {

  private static final List<String> failures = new ArrayList<>();
  private static int checks;

  public static void main(String[] args) {
    Properties settings = ImpactCustomizer.loadSettings();
    check("the build wrote the scope", settings.getProperty("instrumentations", "").contains("openai"));

    // Inside the kit: the scope is cut, metrics are off, logs and traces go to the gateway, whatever the pod says.
    Map<String, String> kitEnv = new HashMap<>();
    kitEnv.put("IMPACT_OTLP_ENDPOINT", "http://impact-ai-gateway.impact-ai.svc:4318/");
    kitEnv.put("OTEL_RESOURCE_ATTRIBUTES", "k8s.namespace.name=shop,k8s.deployment.name=orders,k8s.pod.name=orders-7");
    ImpactCustomizer kit = new ImpactCustomizer(kitEnv, settings);
    Map<String, String> overrides = kit.overrides(null);
    equal("default-enabled", "false", overrides.get("otel.instrumentation.common.default-enabled"));
    for (String name :
        new String[] {
          "tomcat", "java-http-client", "openai-java", "aws-sdk-2.2-core", "aws-sdk-2.2-bedrock-runtime", "executors",
          "opentelemetry-api"
        }) {
      equal(name + " on", "true", overrides.get("otel.instrumentation." + name + ".enabled"));
    }
    for (String name :
        new String[] {
          "jdbc", "logback-appender", "log4j-appender", "java-util-logging", "kafka-clients", "aws-sdk", "aws-sdk-2.2",
          "aws-sdk-2.2-sqs", "aws-sdk-2.2-sns"
        }) {
      check(name + " is not turned on", !overrides.containsKey("otel.instrumentation." + name + ".enabled"));
    }
    // The workload cannot turn an instrumentation outside the scope back on by name, however it asks.
    Map<String, String> widenEnv = new HashMap<>(kitEnv);
    widenEnv.put("OTEL_INSTRUMENTATION_JDBC_ENABLED", "true");
    widenEnv.put("OTEL_INSTRUMENTATION_LOGBACK_APPENDER_ENABLED", "TRUE");
    widenEnv.put("OTEL_INSTRUMENTATION_KAFKA_ENABLED", "false");
    widenEnv.put("OTEL_INSTRUMENTATION_TOMCAT_ENABLED", "true");
    widenEnv.put("OTEL_INSTRUMENTATION_COMMON_DEFAULT_ENABLED", "true");
    Properties widenSystem = new Properties();
    widenSystem.setProperty("otel.instrumentation.log4j-appender.enabled", "true");
    widenSystem.setProperty("otel.instrumentation.servlet.experimental.capture-request-parameters.enabled", "true");
    widenSystem.setProperty("otel.instrumentation.genai.capture-message-content", "true");
    widenSystem.setProperty("otel.instrumentation.micrometer.enabled", "true");
    // names that start with a name of the scope and are other instrumentations, not options of it
    widenEnv.put("OTEL_INSTRUMENTATION_REACTOR_KAFKA_ENABLED", "true");
    widenEnv.put("OTEL_INSTRUMENTATION_TOMCAT_JDBC_ENABLED", "true");
    widenEnv.put("OTEL_INSTRUMENTATION_AWS_SDK_ENABLED", "true");
    widenEnv.put("OTEL_INSTRUMENTATION_AWS_SDK_2_2_SQS_ENABLED", "true");
    widenSystem.setProperty("otel.instrumentation.netty.connection-telemetry.enabled", "true");
    Map<String, String> widened = new ImpactCustomizer(widenEnv, widenSystem, settings).overrides(null);
    for (String name :
        new String[] {
          "jdbc", "logback.appender", "log4j.appender", "micrometer", "reactor.kafka", "tomcat.jdbc", "aws.sdk",
          "aws.sdk.2.2.sqs"
        }) {
      equal(name + " cleared", "", widened.get("otel.instrumentation." + name + ".enabled"));
    }
    check("kafka off already", !widened.containsKey("otel.instrumentation.kafka.enabled"));
    equal("tomcat stays on", "true", widened.get("otel.instrumentation.tomcat.enabled"));
    check("an option of a scoped one stays", !widened.containsKey("otel.instrumentation.servlet.experimental.capture.request.parameters.enabled"));
    check("a known option of a scoped one stays", !widened.containsKey("otel.instrumentation.netty.connection.telemetry.enabled"));
    equal("bedrock stays on", "true", widened.get("otel.instrumentation.aws-sdk-2.2-bedrock-runtime.enabled"));
    equal("default-enabled stays off", "false", widened.get("otel.instrumentation.common.default-enabled"));
    check("default-enabled is not doubled", !widened.containsKey("otel.instrumentation.common.default.enabled"));
    try {
      java.io.File file = java.nio.file.Files.createTempFile("otel", ".properties").toFile();
      file.deleteOnExit();
      java.nio.file.Files.write(file.toPath(), "otel.instrumentation.jms.enabled=true\n".getBytes(java.nio.charset.StandardCharsets.UTF_8));
      Map<String, String> fileEnv = new HashMap<>(kitEnv);
      fileEnv.put("OTEL_JAVAAGENT_CONFIGURATION_FILE", file.getPath());
      equal("a configuration file", "", new ImpactCustomizer(fileEnv, settings).overrides(null).get("otel.instrumentation.jms.enabled"));
      fileEnv.put("OTEL_JAVAAGENT_CONFIGURATION_FILE", file.getPath() + ".missing");
      check("a missing configuration file", new ImpactCustomizer(fileEnv, settings).overrides(null).get("otel.instrumentation.jms.enabled") == null);
    } catch (java.io.IOException e) {
      check("a temporary file: " + e, false);
    }
    equal("metrics", "none", overrides.get("otel.metrics.exporter"));
    equal("logs", "otlp", overrides.get("otel.logs.exporter"));
    equal("traces", "otlp", overrides.get("otel.traces.exporter"));
    equal("traces endpoint", "http://impact-ai-gateway.impact-ai.svc:4318/v1/traces", overrides.get("otel.exporter.otlp.traces.endpoint"));
    equal("logs endpoint", "http://impact-ai-gateway.impact-ai.svc:4318/v1/logs", overrides.get("otel.exporter.otlp.logs.endpoint"));
    equal("headers", "", overrides.get("otel.exporter.otlp.headers"));
    equal("service from the workload", "orders", kit.defaults().get("otel.service.name"));
    equal("content on by default", "true", kit.defaults().get("otel.instrumentation.genai.capture-message-content"));

    // Outside the kit the workload's exporter settings stay; a named service keeps its name.
    Map<String, String> ownEnv = new HashMap<>();
    ownEnv.put("OTEL_SERVICE_NAME", "checkout");
    ImpactCustomizer outside = new ImpactCustomizer(ownEnv, settings);
    check("no endpoint outside the kit", !outside.overrides(null).containsKey("otel.exporter.otlp.endpoint"));
    check("a named service keeps its name", !outside.defaults().containsKey("otel.service.name"));
    Map<String, String> attributeEnv = new HashMap<>();
    attributeEnv.put("OTEL_RESOURCE_ATTRIBUTES", "service.name=billing,k8s.deployment.name=orders");
    check("service.name in the attributes", new ImpactCustomizer(attributeEnv, settings).workloadName() == null);
    equal("distro", "impact-instrumentation", kit.distro().getAttribute(io.opentelemetry.api.common.AttributeKey.stringKey("telemetry.distro.name")));

    // Probes make no server spans; other server spans and every client span keep the agent's decision.
    for (String probe : new String[] {"/healthz", "/health", "/actuator/health", "/actuator/health/liveness", "/readyz", "/livez", "/ping", "/metrics", "/-/ready", "/q/health/live"}) {
      check(probe + " is a probe", ProbeSampler.isProbe(probe));
    }
    for (String path : new String[] {"/chat", "/api/healthy-food", "/pingpong", "/v1/metrics-report", "/", ""}) {
      check(path + " is not a probe", !ProbeSampler.isProbe(path));
    }
    Sampler sampler = new ProbeSampler(Sampler.alwaysOn());
    Attributes probe = Attributes.of(ProbeSampler.URL_PATH, "/actuator/health");
    equal("a probe's server span", SamplingDecision.DROP, sampler.shouldSample(Context.root(), "0af7651916cd43dd8448eb211c80319c", "GET", SpanKind.SERVER, probe, Collections.emptyList()).getDecision());
    equal("a client span to the same path", SamplingDecision.RECORD_AND_SAMPLE, sampler.shouldSample(Context.root(), "0af7651916cd43dd8448eb211c80319c", "GET", SpanKind.CLIENT, probe, Collections.emptyList()).getDecision());
    equal("a request", SamplingDecision.RECORD_AND_SAMPLE, sampler.shouldSample(Context.root(), "0af7651916cd43dd8448eb211c80319c", "POST", SpanKind.SERVER, Attributes.of(ProbeSampler.URL_PATH, "/chat"), Collections.emptyList()).getDecision());

    // Only GenAI events are exported as logs.
    List<LogRecordData> exported = new ArrayList<>();
    GenAiEventsOnly filter = new GenAiEventsOnly(new Recorder(exported));
    filter.export(
        Arrays.asList(
            record("gen_ai.client.inference.operation.details", Attributes.empty()),
            record(null, Attributes.of(GenAiEventsOnly.EVENT_NAME, "gen_ai.user.message")),
            record(null, Attributes.empty()),
            record("", Attributes.empty()),
            record("app.audit", Attributes.empty())));
    equal("GenAI events kept", 2, exported.size());
    exported.clear();
    check("nothing to send is a success", filter.export(Arrays.asList(record(null, Attributes.empty()))).isSuccess() && exported.isEmpty());

    if (!failures.isEmpty()) {
      System.err.println("ExtensionSelfTest: " + failures.size() + " of " + checks + " checks failed:");
      for (String failure : failures) {
        System.err.println("  " + failure);
      }
      System.exit(1);
    }
    System.out.println("ExtensionSelfTest: " + checks + " checks passed");
  }

  private static LogRecordData record(String eventName, Attributes attributes) {
    return (LogRecordData)
        Proxy.newProxyInstance(
            LogRecordData.class.getClassLoader(),
            new Class<?>[] {LogRecordData.class},
            (proxy, method, args) -> {
              switch (method.getName()) {
                case "getEventName":
                  return eventName;
                case "getAttributes":
                  return attributes;
                case "toString":
                  return "record " + eventName;
                default:
                  return null;
              }
            });
  }

  private static final class Recorder implements LogRecordExporter {
    private final List<LogRecordData> into;

    Recorder(List<LogRecordData> into) {
      this.into = into;
    }

    @Override
    public CompletableResultCode export(Collection<LogRecordData> logs) {
      into.addAll(logs);
      return CompletableResultCode.ofSuccess();
    }

    @Override
    public CompletableResultCode flush() {
      return CompletableResultCode.ofSuccess();
    }

    @Override
    public CompletableResultCode shutdown() {
      return CompletableResultCode.ofSuccess();
    }
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
