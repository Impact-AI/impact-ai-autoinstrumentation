// SPDX-License-Identifier: Apache-2.0

package ai.impact.instrumentation.javaagent;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.List;
import java.util.Random;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * The upstream agent and the kit's extension, nested in this jar, as files the JVM can load.
 *
 * <p>The Operator copies one file into the pod. The two jars inside it are written once per pod, next to it in the
 * Operator's volume (or in the temporary directory when that is not writable), under a name that holds their SHA-256,
 * and each is checked against its SHA-256 before it gets that name: a file that has the name is complete and is the
 * one the image was built with. Several JVMs starting at once each write their own copy and rename it into place.
 */
final class Payload {

  static final String AGENT_ENTRY = "impact/opentelemetry-javaagent.jar";
  static final String EXTENSION_ENTRY = "impact/impact-extension.jar";

  private Payload() {}

  static final class Files {
    final File agent;
    final File extension;

    Files(File agent, File extension) {
      this.agent = agent;
      this.extension = extension;
    }
  }

  static Files unpack(File ownJar, Settings settings, Environment env) throws IOException {
    if (ownJar == null || !ownJar.isFile()) {
      throw new IOException("the agent cannot find its own jar");
    }
    String name = ".impact-java-" + settings.agentSha256().substring(0, 12) + "-" + settings.extensionSha256().substring(0, 12);
    List<File> places = new ArrayList<>();
    if (ownJar.getParentFile() != null) {
      places.add(new File(ownJar.getParentFile(), name));
    }
    String tmp = env.property("java.io.tmpdir");
    places.add(new File(tmp.isEmpty() ? "/tmp" : tmp, name));
    IOException last = null;
    try (ZipFile zip = new ZipFile(ownJar)) {
      for (File directory : places) {
        try {
          File agent = place(zip, AGENT_ENTRY, directory, "opentelemetry-javaagent.jar", settings.agentSha256());
          File extension = place(zip, EXTENSION_ENTRY, directory, "impact-extension.jar", settings.extensionSha256());
          return new Files(agent, extension);
        } catch (IOException e) {
          last = e;
        }
      }
    }
    throw new IOException("no writable place for the agent: " + (last == null ? "" : last.getMessage()));
  }

  private static File place(ZipFile zip, String entryName, File directory, String fileName, String sha256)
      throws IOException {
    ZipEntry entry = zip.getEntry(entryName);
    if (entry == null) {
      throw new IOException(entryName + " is missing from the image");
    }
    File target = new File(directory, fileName);
    if (target.isFile() && target.length() == entry.getSize()) {
      return target;
    }
    if (!directory.isDirectory() && !directory.mkdirs() && !directory.isDirectory()) {
      throw new IOException("cannot create " + directory);
    }
    directory.setReadable(true, false);
    directory.setExecutable(true, false);
    File temporary = new File(directory, fileName + "." + Long.toHexString(new Random().nextLong()) + ".tmp");
    try {
      MessageDigest digest = MessageDigest.getInstance("SHA-256");
      try (InputStream in = zip.getInputStream(entry);
          OutputStream out = new FileOutputStream(temporary)) {
        byte[] buffer = new byte[1 << 16];
        int n;
        while ((n = in.read(buffer)) > 0) {
          digest.update(buffer, 0, n);
          out.write(buffer, 0, n);
        }
      }
      String actual = hex(digest.digest());
      if (!actual.equals(sha256)) {
        throw new IOException(entryName + " has SHA-256 " + actual + ", the image was built with " + sha256);
      }
      temporary.setReadable(true, false);
      if (!temporary.renameTo(target) && !(target.isFile() && target.length() == entry.getSize())) {
        throw new IOException("cannot write " + target);
      }
      return target;
    } catch (NoSuchAlgorithmException e) {
      throw new IOException(e.getMessage(), e);
    } finally {
      if (temporary.exists()) {
        temporary.delete();
      }
    }
  }

  static String hex(byte[] bytes) {
    StringBuilder out = new StringBuilder(bytes.length * 2);
    for (byte b : bytes) {
      out.append(Character.forDigit((b >> 4) & 0xf, 16)).append(Character.forDigit(b & 0xf, 16));
    }
    return out.toString();
  }
}
