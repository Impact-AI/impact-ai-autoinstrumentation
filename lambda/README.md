<!-- SPDX-License-Identifier: Apache-2.0 -->

# AWS Lambda: the Python layer

Nothing in this directory is released yet. It builds a Lambda layer that traces Python functions the way the image
traces Python workloads, with one change to a function: the layer, and
`AWS_LAMBDA_EXEC_WRAPPER=/opt/impact-ai-instrument` with the kit's environment variables
([`environment.json`](environment.json)).

* **What the layer holds.** `make lambda-layers` builds one archive per architecture from the payload the Python image
  carries ([`build_layer.py`](build_layer.py)): the wrapper, the package's start, the payload, its licenses and an SPDX
  SBOM, laid out under `/opt/impact-ai`, so a function sees none of the layer's packages unless the package starts. The
  archive is built the same way twice from the same inputs, and `build/lambda/SIZES.md` sets it against Lambda's limits.
* **How it runs.** The wrapper puts the layer's start first on `PYTHONPATH`; the package starts before the runtime loads
  the function, wraps its handler, and sends what it holds at the end of each invocation, waiting a bounded time. It
  stands down beside another tracer's Lambda wrapper, and whatever happens, the function runs and returns as it would
  without the layer ([`aws_lambda.py`](../python/src/impact_instrumentation/aws_lambda.py)).
* **How it is tested.** `make test-lambda` runs the layer in AWS's Lambda images for Python 3.11 to 3.14 under their
  Runtime Interface Emulator: Bedrock spans with the function's resource, fail open, stand-down. `make lambda-timings`
  measures cold starts and invocations with and without the layer.
