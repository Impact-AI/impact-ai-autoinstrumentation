# SPDX-License-Identifier: Apache-2.0

"""One call to each of OpenAI, Anthropic and Amazon Bedrock, traced by Datadog's Python tracer (ddtrace).

    DD_TRACE_AGENT_URL=http://<agent or relay> DD_LLMOBS_ENABLED=1 DD_LLMOBS_ML_APP=support-assistant \\
    DD_SERVICE=support-assistant PROVIDER_URL=http://<recording mock> python providers.py

PROVIDER_URL is the kit's recording mock (testing/mock/server.py), which answers each provider's API with fixed
answers. ddtrace names the OpenAI and Anthropic providers from the client's base URL, hence the /openai and /anthropic
paths; the mock answers any path that ends in its provider's API path. The keys and AWS credentials are planted,
fictitious ones. Each call carries CONTENT-CANARY in its prompt.
"""

import ddtrace.auto  # noqa: F401  # isort: skip

import os

import anthropic
import boto3
import openai
from ddtrace.llmobs import LLMObs

CANARY = "CONTENT-CANARY"


def main() -> None:
    url = os.environ["PROVIDER_URL"].rstrip("/")
    question = f"Where is order 1042? {CANARY}"
    openai.OpenAI(base_url=f"{url}/openai/v1", api_key="planted-openai-key").chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "Answer briefly."}, {"role": "user", "content": question}],
    )
    anthropic.Anthropic(base_url=f"{url}/anthropic", api_key="planted-anthropic-key").messages.create(
        model="claude-sonnet-4-5",
        max_tokens=100,
        system="Answer briefly.",
        messages=[{"role": "user", "content": question}],
    )
    boto3.client(
        "bedrock-runtime",
        endpoint_url=url,
        region_name="us-east-1",
        aws_access_key_id="AKIAPLANTED0000000000",
        aws_secret_access_key="planted-aws-secret",  # noqa: S106 - a fictitious credential the mock never checks
    ).converse(
        modelId="amazon.nova-micro-v1:0",
        system=[{"text": "Answer briefly."}],
        messages=[{"role": "user", "content": [{"text": question}]}],
        inferenceConfig={"maxTokens": 100},
    )
    if LLMObs.enabled:
        LLMObs.flush()


if __name__ == "__main__":
    main()
