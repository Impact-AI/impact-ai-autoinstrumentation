// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent;

import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Properties;

/**
 * What the build wrote into the image ({@code impact.properties} beside this class): the kit's version, the Java versions
 * the image is built for, the nested files with their SHA-256, and the instrumentations the extension turns on.
 */
final class Settings {

  static final String RESOURCE = "impact.properties";

  private final Properties values;

  Settings(Properties values) {
    this.values = values;
  }

  static Settings load() throws IOException {
    Properties values = new Properties();
    try (InputStream in = Settings.class.getResourceAsStream(RESOURCE)) {
      if (in == null) {
        throw new IOException(RESOURCE + " is missing from the image");
      }
      values.load(in);
    }
    return new Settings(values);
  }

  String get(String key) {
    String value = values.getProperty(key);
    if (value == null || value.trim().isEmpty()) {
      throw new IllegalStateException(RESOURCE + " has no " + key);
    }
    return value.trim();
  }

  String version() {
    return get("version");
  }

  /** The feature releases of Java the image is built for, from the build argument JAVA_VERSIONS: 17, 21 and 25. */
  List<Integer> javaVersions() {
    List<Integer> out = new ArrayList<>();
    for (String part : get("javaVersions").split(",")) {
      if (!part.trim().isEmpty()) {
        out.add(Integer.valueOf(part.trim()));
      }
    }
    Collections.sort(out);
    return out;
  }

  /** "Impact AI traces Java 17, 21 and 25; this workload runs Java 11.0.29": what the row says, as for Node.js. */
  String unsupported(String running) {
    List<Integer> versions = javaVersions();
    StringBuilder list = new StringBuilder();
    for (int i = 0; i < versions.size(); i++) {
      if (i > 0) {
        list.append(i == versions.size() - 1 ? " and " : ", ");
      }
      list.append(versions.get(i));
    }
    return SetupReport.cut("Impact AI traces Java " + list + "; this workload runs Java " + running);
  }

  /** The instrumentations the extension turns on, comma separated, for the setup report. */
  String instrumentations() {
    return get("instrumentations");
  }

  String agentSha256() {
    return get("agentSha256");
  }

  String extensionSha256() {
    return get("extensionSha256");
  }
}
