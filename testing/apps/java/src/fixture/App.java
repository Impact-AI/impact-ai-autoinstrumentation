// SPDX-License-Identifier: Apache-2.0

package fixture;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;
import java.util.logging.Logger;

/**
 * A plain Java workload without tracing of its own: the JDK's HTTP server and client, nothing else on the class path.
 *
 * <pre>
 *   GET  /healthz   200 "ok" (what a probe calls)
 *   POST /chat      calls an OpenAI-compatible chat completion at $STUB_URL and returns its answer
 *   POST /log       writes an application log line (java.util.logging) and returns 200
 *   GET  /agent     the value the fake APM agent sets when it runs, or "none"
 * </pre>
 */
public final class App {
  private static final Logger LOG = Logger.getLogger("fixture.App");

  public static void main(String[] args) throws IOException {
    int port = Integer.parseInt(System.getenv().getOrDefault("PORT", "8000"));
    String stub = System.getenv().getOrDefault("STUB_URL", "http://localhost:8080");
    HttpClient client = HttpClient.newHttpClient();
    HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
    server.setExecutor(Executors.newFixedThreadPool(4));
    server.createContext("/healthz", exchange -> answer(exchange, 200, "ok"));
    server.createContext("/agent", exchange -> answer(exchange, 200, System.getProperty("fakeapm.started", "none")));
    server.createContext(
        "/log",
        exchange -> {
          LOG.warning("an application log line that must stay in the pod");
          answer(exchange, 200, "logged");
        });
    server.createContext(
        "/chat",
        exchange -> {
          String body =
              "{\"model\":\"gpt-4o-mini\",\"messages\":[{\"role\":\"user\",\"content\":\"hi there\"}]}";
          HttpRequest request =
              HttpRequest.newBuilder(URI.create(stub + "/v1/chat/completions"))
                  .header("Content-Type", "application/json")
                  .POST(HttpRequest.BodyPublishers.ofString(body))
                  .build();
          try {
            HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
            answer(exchange, response.statusCode(), response.body());
          } catch (IOException | InterruptedException error) {
            answer(exchange, 502, error.toString());
          }
        });
    server.start();
    System.out.println("listening on " + port);
  }

  private static void answer(HttpExchange exchange, int status, String text) throws IOException {
    byte[] bytes = text.getBytes(StandardCharsets.UTF_8);
    exchange.sendResponseHeaders(status, bytes.length);
    try (OutputStream out = exchange.getResponseBody()) {
      out.write(bytes);
    }
  }
}
