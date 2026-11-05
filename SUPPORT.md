# Supported versions and updates

Which versions of Python, Node.js and Kubernetes the kit supports, and how it stays current. To report a vulnerability,
see [SECURITY.md](SECURITY.md).

## Runtimes the kit instruments

The kit supports the versions of Python and Node.js that their own projects still support.

| Runtime | Supported by the kit | End of life upstream |
|---|---|---|
| Python | 3.11 | October 2027 |
| | 3.12 | October 2028 |
| | 3.13 | October 2029 |
| | 3.14 | October 2030 |
| Node.js | 22, from 22.15 | 30 April 2027 |
| | 24 | 30 April 2028 |
| | 26 | 30 April 2029 |

* Node.js 22.15 or later; an older Node starts the application as without the kit, and the setup report says why.
  Tested on 22, 24 and 26, CommonJS and ES modules.
* Node.js means its LTS lines, which is what OpenTelemetry JavaScript supports.
* A version leaves in the first kit release after its upstream end of life, and the release before announces it.
* A new version joins once OpenTelemetry supports it and our tests pass.
* On 4 November 2026: Node.js 26 became an LTS line on 28 October 2026, which OpenTelemetry JavaScript supports.
  Python 3.15 was released on 9 October 2026. OpenTelemetry Python lists 3.10 to 3.14 and adds a new Python version no
  later than three months after it is stable, so 3.15 joins the kit when OpenTelemetry supports it and our tests pass.

## Kubernetes

The kit supports the Kubernetes versions that the Kubernetes project supports, and the older ones that Amazon EKS,
Azure AKS or Google GKE still support in extended or long-term support.

* On 4 November 2026 the Kubernetes project supports 1.35, 1.36 and 1.37 (1.34 reached its end of life on 27 October
  2026), and extended or long-term support at EKS, AKS and GKE reaches back to 1.31. At EKS and AKS that ends in November
  2026, and 1.32 is then the oldest.
* The chart installs on Kubernetes 1.27 and later (`kubeVersion` in `Chart.yaml`). CI runs every change on Kubernetes
  1.27 and 1.34: the end-to-end suite on 1.34 and its Kubernetes part on 1.27.

## Dependencies

Dependabot opens update pull requests every week for

* the base images of every Dockerfile, pinned by digest (the digest and the patch version move; a new minor or major
  version of an image is a decision, not an update),
* the Go modules of the reporter,
* the npm packages of the Node.js image,
* the Python packages of the development environment, and
* the GitHub Actions of the workflows.

The OpenTelemetry packages of an ecosystem arrive together in one pull request. CI decides whether an update merges,
and a fix reaches customers in the next patch release.

## Security fixes

* Reports are acknowledged within 3 business days, as [SECURITY.md](SECURITY.md) says.
* Vulnerabilities are fixed without undue delay, the most severe and the actively exploited first.
* Fixes go into the latest release.
* Each fix is announced as a GitHub security advisory, with a CVE where one applies, and disclosure is coordinated with
  the reporter.
* A finding that does not affect the kit is documented with its reason (VEX).

## Versions

Releases follow [Semantic Versioning](https://semver.org). Any removal is announced one minor release ahead.
