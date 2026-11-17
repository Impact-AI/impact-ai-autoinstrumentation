# Documentation

The [chart README](../chart/impact-ai/README.md) is the place to start: requirements, install, values, verifying a
release, upgrade and uninstall. These pages hold the detail.

| Page | Covers |
|---|---|
| [Install options](install.md) | The install token step by step, giving the key yourself (GitOps), cloud identity, image mirrors, a new key |
| [How injection works](how-injection-works.md) | The annotation, automatic coverage, what is traced in Python and Node.js, agents, the setup report, log events |
| [Coexistence and stand-down](coexistence.md) | Beside another injector in the cluster, beside another tracer in the process |
| [What leaves the cluster](what-leaves-the-cluster.md) | Every field sent, the content default and how to turn content off |
| [Failure modes](failure-modes.md) | What happens to your workloads when a part of the kit or Impact AI is unavailable |
| [Security model](security-model.md) | RBAC of every component, network flows, admission rules, the install token's exposure |
| [Troubleshooting](troubleshooting.md) | Symptoms, causes and fixes, and what to put in a bug report |
| [Compatibility](compatibility.md) | Runtimes, libraries and Kubernetes versions supported and tested, overhead, and the version policy |

Building, testing and releasing the kit: [CONTRIBUTING.md](../CONTRIBUTING.md).
