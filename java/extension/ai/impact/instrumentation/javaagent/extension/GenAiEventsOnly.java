// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent.extension;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.sdk.common.CompletableResultCode;
import io.opentelemetry.sdk.logs.data.LogRecordData;
import io.opentelemetry.sdk.logs.export.LogRecordExporter;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;

/**
 * Only GenAI events leave the pod as logs: a record is exported when its event name (or the older {@code event.name}
 * attribute) starts with {@code gen_ai.}. The logging appenders are off as well, so this is the second of two locks on
 * the application's own log lines.
 */
final class GenAiEventsOnly implements LogRecordExporter {

  static final AttributeKey<String> EVENT_NAME = AttributeKey.stringKey("event.name");

  private final LogRecordExporter delegate;

  GenAiEventsOnly(LogRecordExporter delegate) {
    this.delegate = delegate;
  }

  static boolean isGenAiEvent(LogRecordData record) {
    String name = record.getEventName();
    if (name == null || name.isEmpty()) {
      name = record.getAttributes().get(EVENT_NAME);
    }
    return name != null && name.startsWith("gen_ai.");
  }

  @Override
  public CompletableResultCode export(Collection<LogRecordData> logs) {
    List<LogRecordData> kept = new ArrayList<>(logs.size());
    for (LogRecordData record : logs) {
      if (isGenAiEvent(record)) {
        kept.add(record);
      }
    }
    return kept.isEmpty() ? CompletableResultCode.ofSuccess() : delegate.export(kept);
  }

  @Override
  public CompletableResultCode flush() {
    return delegate.flush();
  }

  @Override
  public CompletableResultCode shutdown() {
    return delegate.shutdown();
  }

  @Override
  public String toString() {
    return "GenAiEventsOnly{" + delegate + "}";
  }
}
