// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Proxy;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;
import java.util.zip.Adler32;
import java.util.zip.CRC32;

/**
 * The {@code impact.setup} span: one report per container naming the state the image is in, as the Python and Node.js
 * images send it. States: {@code active} (the agent started), {@code pre-existing} (it stood down beside another agent;
 * the detail names it) and {@code failed} (it did not start; the detail says why, a Java outside the range included).
 *
 * <p>It is built and sent with the JDK alone, in OTLP/HTTP JSON over a plain socket, from a daemon thread: no class of
 * OpenTelemetry, no logging framework and no URL handler is touched, so that it works in all three states and leaves the
 * application's own setup as it was. A stood-down workload reports to the kit's gateway only: its OTEL_EXPORTER_OTLP_*
 * settings belong to the other agent. Only {@code http} endpoints are reported to; the kit's gateway is one.
 */
final class SetupReport {

  static final String SPAN_NAME = "impact.setup";
  static final int MAX_DETAIL = 200;
  static final String KIT_ENDPOINT = "IMPACT_OTLP_ENDPOINT";
  static final int KIT_TIMEOUT_MS = 2000;
  static final int DEFAULT_TIMEOUT_MS = 10000;
  static final long EXIT_GRACE_MS = 500;
  static final long RETRY_SLEEP_MS = 200;
  static final String MARKER_PREFIX = ".impact-setup-";

  private static final String[] WORKLOAD_NAME_KEYS = {
    "k8s.deployment.name",
    "k8s.statefulset.name",
    "k8s.daemonset.name",
    "k8s.cronjob.name",
    "k8s.job.name",
    "k8s.pod.name",
  };

  private SetupReport() {}

  static String cut(String text) {
    return text.length() <= MAX_DETAIL ? text : text.substring(0, MAX_DETAIL - 3) + "...";
  }

  /** Claim and send a report, once per container for the same content. Never throws. */
  static Thread report(String state, String detail, Settings settings, Environment env, String instrumentations) {
    try {
      String version = settings == null ? "unknown" : settings.version();
      if ("pre-existing".equals(state) && !inKit(env)) {
        return null;
      }
      String url = tracesEndpoint(env);
      if (!url.startsWith("http://")) {
        return null;
      }
      String content = state + "\0" + detail + "\0" + (instrumentations == null ? "" : instrumentations);
      if (!claim(content, env)) {
        return null;
      }
      final byte[] body = build(state, detail, version, env, instrumentations).getBytes(StandardCharsets.UTF_8);
      final URI target = URI.create(url);
      final int timeout = inKit(env) ? KIT_TIMEOUT_MS : DEFAULT_TIMEOUT_MS;
      final String userAgent = "impact-instrumentation/" + version + " java/" + env.javaVersion();
      final Thread thread =
          new Thread(
              new Runnable() {
                @Override
                public void run() {
                  for (int attempt = 0; attempt < 2; attempt++) {
                    try {
                      if (post(target, body, timeout, userAgent)) {
                        return;
                      }
                    } catch (Throwable ignored) {
                      // best effort
                    }
                    try {
                      Thread.sleep(RETRY_SLEEP_MS);
                    } catch (InterruptedException e) {
                      return;
                    }
                  }
                }
              },
              "impact-setup-report");
      thread.setDaemon(true);
      thread.start();
      try {
        Runtime.getRuntime()
            .addShutdownHook(
                new Thread(
                    new Runnable() {
                      @Override
                      public void run() {
                        try {
                          thread.join(EXIT_GRACE_MS);
                        } catch (InterruptedException ignored) {
                          // the process is ending anyway
                        }
                      }
                    },
                    "impact-setup-report-exit"));
      } catch (IllegalStateException ignored) {
        // already shutting down
      }
      return thread;
    } catch (Throwable ignored) {
      return null;
    }
  }

  static boolean inKit(Environment env) {
    return !env.variable(KIT_ENDPOINT).trim().isEmpty();
  }

  static String tracesEndpoint(Environment env) {
    String kit = env.variable(KIT_ENDPOINT).trim();
    if (!kit.isEmpty()) {
      return stripSlashes(kit) + "/v1/traces";
    }
    String specific = env.variable("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT").trim();
    if (!specific.isEmpty()) {
      return specific;
    }
    String general = env.variable("OTEL_EXPORTER_OTLP_ENDPOINT").trim();
    return (general.isEmpty() ? "http://localhost:4318" : stripSlashes(general)) + "/v1/traces";
  }

  private static String stripSlashes(String url) {
    String out = url;
    while (out.endsWith("/")) {
      out = out.substring(0, out.length() - 1);
    }
    return out;
  }

  /**
   * True for the first process of the container to report this content; false for the others. The marker lives in
   * IMPACT_STATE_DIR (default /tmp); where it cannot be written the report is sent anyway: a duplicate is harmless, a
   * missing report is not.
   */
  static boolean claim(String content, Environment env) {
    byte[] data = content.getBytes(StandardCharsets.UTF_8);
    CRC32 crc = new CRC32();
    crc.update(data, 0, data.length);
    Adler32 adler = new Adler32();
    adler.update(data, 0, data.length);
    String key = String.format("%08x%08x", crc.getValue(), adler.getValue());
    String directory = env.variable("IMPACT_STATE_DIR").trim();
    File marker = new File(directory.isEmpty() ? "/tmp" : directory, MARKER_PREFIX + "java-" + key);
    try {
      return marker.createNewFile();
    } catch (IOException | SecurityException e) {
      return true;
    }
  }

  static String build(String state, String detail, String version, Environment env, String instrumentations) {
    long now = System.currentTimeMillis() * 1_000_000L;
    Map<String, Object> attributes = new LinkedHashMap<>();
    attributes.put("impact.setup.state", state);
    attributes.put("impact.setup.detail", cut(detail));
    attributes.put("impact.setup.package.version", version);
    attributes.put("impact.setup.java.version", env.javaVersion());
    attributes.put("impact.setup.frameworks", new String[0]);
    attributes.put("impact.setup.untraced", new String[0]);
    if (instrumentations != null) {
      attributes.put("impact.setup.instrumentations", instrumentations);
    }
    StringBuilder json = new StringBuilder(1024);
    json.append("{\"resourceSpans\":[{\"resource\":{\"attributes\":");
    keyValues(json, resourceAttributes(env, version));
    json.append("},\"scopeSpans\":[{\"scope\":{\"name\":");
    string(json, SPAN_NAME);
    json.append(",\"version\":");
    string(json, version);
    json.append("},\"spans\":[{\"traceId\":\"").append(randomHex(16)).append("\",\"spanId\":\"").append(randomHex(8));
    json.append("\",\"name\":\"").append(SPAN_NAME).append("\",\"kind\":1,\"startTimeUnixNano\":\"").append(now);
    json.append("\",\"endTimeUnixNano\":\"").append(now).append("\",\"attributes\":");
    keyValues(json, attributes);
    json.append(",\"status\":{},\"flags\":256}]}]}]}");
    return json.toString();
  }

  /** The workload's resource as the agent builds it from OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME. */
  static Map<String, Object> resourceAttributes(Environment env, String version) {
    Map<String, Object> attributes = new LinkedHashMap<>();
    for (String pair : env.variable("OTEL_RESOURCE_ATTRIBUTES").split(",")) {
      int at = pair.indexOf('=');
      if (at <= 0) {
        continue;
      }
      String key = pair.substring(0, at).trim();
      if (!key.isEmpty()) {
        attributes.put(key, percentDecode(pair.substring(at + 1).trim()));
      }
    }
    String service = env.variable("OTEL_SERVICE_NAME").trim();
    if (!service.isEmpty()) {
      attributes.put("service.name", service);
    }
    if (!attributes.containsKey("service.name")) {
      String name = "unknown_service:java";
      for (String key : WORKLOAD_NAME_KEYS) {
        Object value = attributes.get(key);
        if (value != null && !value.toString().isEmpty()) {
          name = value.toString();
          break;
        }
      }
      attributes.put("service.name", name);
    }
    attributes.put("telemetry.sdk.language", "java");
    attributes.put("telemetry.distro.name", "impact-instrumentation");
    attributes.put("telemetry.distro.version", version);
    return attributes;
  }

  static String percentDecode(String text) {
    if (text.indexOf('%') < 0) {
      return text;
    }
    try {
      return java.net.URLDecoder.decode(text.replace("+", "%2B"), "UTF-8");
    } catch (Exception e) {
      return text;
    }
  }

  private static void keyValues(StringBuilder json, Map<String, Object> attributes) {
    json.append('[');
    boolean first = true;
    for (Map.Entry<String, Object> entry : attributes.entrySet()) {
      if (!first) {
        json.append(',');
      }
      first = false;
      json.append("{\"key\":");
      string(json, entry.getKey());
      json.append(",\"value\":");
      Object value = entry.getValue();
      if (value instanceof String[]) {
        json.append("{\"arrayValue\":{\"values\":[");
        String[] items = (String[]) value;
        for (int i = 0; i < items.length; i++) {
          if (i > 0) {
            json.append(',');
          }
          json.append("{\"stringValue\":");
          string(json, items[i]);
          json.append('}');
        }
        json.append("]}}");
      } else {
        json.append("{\"stringValue\":");
        string(json, String.valueOf(value));
        json.append('}');
      }
      json.append('}');
    }
    json.append(']');
  }

  static void string(StringBuilder json, String text) {
    json.append('"');
    for (int i = 0; i < text.length(); i++) {
      char c = text.charAt(i);
      switch (c) {
        case '"':
          json.append("\\\"");
          break;
        case '\\':
          json.append("\\\\");
          break;
        case '\n':
          json.append("\\n");
          break;
        case '\r':
          json.append("\\r");
          break;
        case '\t':
          json.append("\\t");
          break;
        default:
          if (c < 0x20) {
            json.append(String.format("\\u%04x", (int) c));
          } else {
            json.append(c);
          }
      }
    }
    json.append('"');
  }

  private static String randomHex(int bytes) {
    byte[] out = new byte[bytes];
    ThreadLocalRandom.current().nextBytes(out);
    return Payload.hex(out);
  }

  /** One HTTP/1.1 POST over a plain socket; true when the gateway answered 2xx. */
  static boolean post(URI target, byte[] body, int timeoutMs, String userAgent) throws IOException {
    String host = target.getHost();
    int port = target.getPort() < 0 ? 80 : target.getPort();
    String path = target.getRawPath() == null || target.getRawPath().isEmpty() ? "/" : target.getRawPath();
    if (target.getRawQuery() != null) {
      path += "?" + target.getRawQuery();
    }
    try (Socket socket = new Socket(Proxy.NO_PROXY)) {
      socket.connect(new InetSocketAddress(host, port), timeoutMs);
      socket.setSoTimeout(timeoutMs);
      String head =
          "POST " + path + " HTTP/1.1\r\n"
              + "Host: " + host + ":" + port + "\r\n"
              + "User-Agent: " + userAgent + "\r\n"
              + "Content-Type: application/json\r\n"
              + "Content-Length: " + body.length + "\r\n"
              + "Connection: close\r\n\r\n";
      OutputStream out = socket.getOutputStream();
      out.write(head.getBytes(StandardCharsets.ISO_8859_1));
      out.write(body);
      out.flush();
      InputStream in = socket.getInputStream();
      byte[] status = new byte[12];
      int read = 0;
      while (read < status.length) {
        int n = in.read(status, read, status.length - read);
        if (n < 0) {
          break;
        }
        read += n;
      }
      String line = new String(status, 0, read, StandardCharsets.ISO_8859_1);
      return line.startsWith("HTTP/1.") && line.length() >= 12 && line.charAt(9) == '2';
    }
  }
}
