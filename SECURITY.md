# Security policy

## Report a vulnerability

Please report a vulnerability privately, not as a public issue:

* In GitHub: [Report a vulnerability](https://github.com/Impact-AI/impact-autoinstrumentation/security/advisories/new), under
  the Security tab of this repository.
* By e-mail: security@impact.ai

Say which version you looked at (the chart version from `helm list -n impact-ai`, or the image tag), what you did and
what you saw. We reply within 3 business days, tell you what we found, and agree the disclosure date with you before
anything is published. If you wish, the advisory credits you.

## What this covers

What this repository publishes: the Helm chart `impact-ai` and the images `autoinstrumentation-python`,
`autoinstrumentation-nodejs` and `kubernetes-reporter` at `ghcr.io/impact-ai`, and the scripts that build and test
them. The repository also builds `autoinstrumentation-java`, which no release publishes yet and the chart does not
inject; reports about it are welcome too. It carries the OpenTelemetry Java agent unchanged, pinned by its SHA-256:
report a vulnerability in the agent itself to the OpenTelemetry project, and one in how the image starts or configures
it to us.

The chart also installs two images that are not ours: the OpenTelemetry Operator and the OpenTelemetry Collector. They
are the OpenTelemetry project's, pinned by digest in the chart. Report a vulnerability in them to that project; when it
releases a fix we bump the digest. Report to us how the chart installs or configures them.

## Supported versions

Security fixes go into the latest release. To get one, upgrade to the new chart version (the chart README has the
steps). [SUPPORT.md](SUPPORT.md) lists the versions of Python, Node.js and Kubernetes the kit
supports and how it stays current.

| Version | Security fixes |
|---|---|
| The latest release | yes |
| Earlier releases | upgrade to the latest |
