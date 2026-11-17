// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent.extension;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.trace.SpanKind;
import io.opentelemetry.context.Context;
import io.opentelemetry.sdk.trace.data.LinkData;
import io.opentelemetry.sdk.trace.samplers.Sampler;
import io.opentelemetry.sdk.trace.samplers.SamplingResult;
import java.util.List;
import java.util.regex.Pattern;

/**
 * Health probes and metric scrapes make no spans: Kubernetes calls them every few seconds. The paths are those the
 * Python and Node.js images exclude. Every other decision is the sampler's the agent configured.
 */
final class ProbeSampler implements Sampler {

  static final AttributeKey<String> URL_PATH = AttributeKey.stringKey("url.path");
  static final Pattern PROBE =
      Pattern.compile(
          "/(health(z|check)?|live(z|ness)?|read(y|yz|iness)|ping|_health|-/(healthy|ready)|metrics)(/.*)?$");

  private final Sampler delegate;

  ProbeSampler(Sampler delegate) {
    this.delegate = delegate;
  }

  static boolean isProbe(String path) {
    return path != null && PROBE.matcher(path).find();
  }

  @Override
  public SamplingResult shouldSample(
      Context parentContext,
      String traceId,
      String name,
      SpanKind spanKind,
      Attributes attributes,
      List<LinkData> parentLinks) {
    if (spanKind == SpanKind.SERVER && isProbe(attributes.get(URL_PATH))) {
      return SamplingResult.drop();
    }
    return delegate.shouldSample(parentContext, traceId, name, spanKind, attributes, parentLinks);
  }

  @Override
  public String getDescription() {
    return "ImpactProbeSampler{" + delegate.getDescription() + "}";
  }
}
