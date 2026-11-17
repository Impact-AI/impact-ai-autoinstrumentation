# Support

## Where to ask

* **A security problem**: do not open an issue. Use the private channel in [SECURITY.md](SECURITY.md).
* **A bug, or something wrong in the documentation**: open an issue in this repository with the bug form. It asks for
  the chart version (`helm list -n impact-ai`), the Kubernetes version and distribution, and the output of
  `kubectl -n impact-ai get pods` and of the reporter's log; [troubleshooting](docs/troubleshooting.md#what-to-collect-first)
  has the commands.
* **A feature request**: open an issue with the feature form.
* **A question about your Impact AI account, product or data**: ask Impact AI support through the channel of your
  subscription. Issues here are public: put no customer data, keys, tokens or cluster names you consider confidential
  into them.

Issues are read by the maintainers at Impact AI. There is no response time for issues; security reports have one
(below).

## What is supported

* The latest release of the chart `impact-ai` and of the images it installs from `ghcr.io/impact-ai`. Fixes go into the
  next release; earlier releases are not patched.
* The runtimes, libraries and Kubernetes versions in [compatibility](docs/compatibility.md), which also says which of
  them CI tests and the policy for adding and removing versions.
* The OpenTelemetry Operator and the OpenTelemetry Collector that the chart installs are the OpenTelemetry project's,
  pinned by digest. A problem in how the chart installs or configures them is ours; a problem in them is that project's.
* A workload's own tracer is not ours: the kit stands down beside it ([coexistence](docs/coexistence.md)).

## Security fixes

* Reports are acknowledged within 3 business days, as [SECURITY.md](SECURITY.md) says.
* Vulnerabilities are fixed without undue delay, the most severe and the actively exploited first.
* Fixes go into the latest release.
* Each fix is announced as a GitHub security advisory, with a CVE where one applies, and disclosure is coordinated with
  the reporter.
* A finding that does not affect the kit is documented with its reason (VEX).

## Versions

Before 1.0, a minor release may contain breaking changes; each is listed first in the [changelog](CHANGELOG.md) with
its upgrade step. Pin `--version` and read the changelog before an upgrade. The full policy is in
[compatibility](docs/compatibility.md#versions).
