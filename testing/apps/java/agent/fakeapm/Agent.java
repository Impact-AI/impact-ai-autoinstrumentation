// SPDX-License-Identifier: Apache-2.0

package fakeapm;

import java.lang.instrument.Instrumentation;

/**
 * A stand-in for an APM vendor's Java agent (dd-java-agent.jar in the tests): it starts, says so in a system property the
 * fixture reports at GET /agent, and does nothing else.
 */
public final class Agent {
  private Agent() {}

  public static void premain(String args, Instrumentation inst) {
    System.setProperty("fakeapm.started", "yes");
    System.err.println("fake APM agent started");
  }
}
