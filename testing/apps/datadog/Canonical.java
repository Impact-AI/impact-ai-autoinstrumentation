// SPDX-License-Identifier: Apache-2.0

// The canonical interaction, traced by Datadog's Java agent (dd-java-agent) with LLM Observability's Java API.
//
// An agent `support` asks `gpt-4o` where an order is; the model asks for the tool `get_order`; the tool runs; a
// retrieval from `help-centre` returns three documents of a top-k of 5; the model answers. Content carries
// CONTENT-CANARY. The model calls go over java.net.http to the canonical provider (canonical_provider.py) and are
// annotated with the API, as an application without a supported OpenAI client library does.
//
//   DD_TRACE_AGENT_URL=http://<agent or relay> DD_LLMOBS_ENABLED=true DD_LLMOBS_ML_APP=support-assistant \
//   DD_SERVICE=support-assistant OPENAI_BASE_URL=http://<canonical_provider>/openai/v1 \
//   java -javaagent:dd-java-agent.jar -cp dd-trace-api.jar Canonical.java
//
// Standard library and dd-trace-api only, so the JDK's source launcher runs it without a build.

import datadog.trace.api.llmobs.LLMObs;
import datadog.trace.api.llmobs.LLMObsSpan;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class Canonical {
  static final String CANARY = "CONTENT-CANARY";
  static final String APP = "support-assistant";
  static final HttpClient HTTP = HttpClient.newHttpClient();

  /** One chat completion: the raw answer of the provider. */
  static String chat(String messages) throws Exception {
    String base = System.getenv().getOrDefault("OPENAI_BASE_URL", "http://127.0.0.1:8091/openai/v1");
    String body = "{\"model\":\"gpt-4o\",\"messages\":" + messages + "}";
    HttpRequest request =
        HttpRequest.newBuilder(URI.create(base + "/chat/completions"))
            .header("Content-Type", "application/json")
            .header("Authorization", "Bearer planted-openai-key")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build();
    return HTTP.send(request, HttpResponse.BodyHandlers.ofString()).body();
  }

  static String field(String json, String pattern) {
    Matcher m = Pattern.compile(pattern).matcher(json);
    return m.find() ? m.group(1) : "";
  }

  static LLMObsSpan model(String answer, String finish) {
    LLMObsSpan span = LLMObs.startLLMSpan("chat", "gpt-4o-2024-08-06", "openai", APP, null);
    span.setMetric("input_tokens", Integer.parseInt(field(answer, "\"prompt_tokens\":\\s*(\\d+)")));
    span.setMetric("output_tokens", Integer.parseInt(field(answer, "\"completion_tokens\":\\s*(\\d+)")));
    span.setMetadata(Map.of("finish_reason", finish));
    return span;
  }

  public static void main(String[] args) throws Exception {
    String question = "Where is order 1042? " + CANARY;
    String user = "{\"role\":\"user\",\"content\":\"" + question + "\"}";
    LLMObsSpan agent = LLMObs.startAgentSpan("support", APP, null);

    String first = chat("[" + user + "]");
    String callId = field(first, "\"id\":\\s*\"(call_[^\"]+)\"");
    String arguments = "1042 " + CANARY;
    LLMObsSpan asked = model(first, "tool_calls");
    asked.annotateIO(
        List.of(LLMObs.LLMMessage.from("user", question)),
        List.of(
            LLMObs.LLMMessage.from(
                "assistant",
                "",
                List.of(LLMObs.ToolCall.from("get_order", "function", callId, Map.of("order_id", arguments))))));
    asked.finish();

    LLMObsSpan tool = LLMObs.startToolSpan("get_order", APP, null);
    String result = "{\"status\":\"shipped\",\"note\":\"" + CANARY + "\"}";
    tool.annotateIO("{\"order_id\":\"" + arguments + "\"}", result);
    tool.finish();

    LLMObsSpan retrieval = LLMObs.startRetrievalSpan("help-centre", APP, null);
    retrieval.annotateRetrievalIO(
        "When does an order ship? " + CANARY,
        List.of(
            LLMObs.Document.from("Orders ship within two days. " + CANARY, "Shipping times", "kb-112", 0.91),
            LLMObs.Document.from("Track an order from its page. " + CANARY, "Tracking", "kb-407", 0.73),
            LLMObs.Document.from("Return an order within 30 days. " + CANARY, "Returns", "kb-019", 0.52)));
    retrieval.setMetadata(Map.of("top_k", 5));
    retrieval.finish();

    String toolCall =
        "{\"role\":\"assistant\",\"content\":null,\"tool_calls\":[{\"id\":\""
            + callId
            + "\",\"type\":\"function\",\"function\":{\"name\":\"get_order\",\"arguments\":\"{\\\"order_id\\\":\\\""
            + arguments
            + "\\\"}\"}}]}";
    String toolMessage =
        "{\"role\":\"tool\",\"tool_call_id\":\"" + callId + "\",\"content\":\"" + result.replace("\"", "\\\"") + "\"}";
    String second = chat("[" + user + "," + toolCall + "," + toolMessage + "]");
    String reply = field(second, "\"content\":\\s*\"([^\"]*)\"");
    LLMObsSpan answered = model(second, "stop");
    answered.annotateIO(
        List.of(LLMObs.LLMMessage.from("user", question), LLMObs.LLMMessage.from("tool", result)),
        List.of(LLMObs.LLMMessage.from("assistant", reply)));
    answered.finish();

    agent.annotateIO(question, reply);
    agent.finish();
    // The agent's writer flushes on its own schedule and at exit; give it the time a short-lived job needs.
    Thread.sleep(3000);
  }
}
