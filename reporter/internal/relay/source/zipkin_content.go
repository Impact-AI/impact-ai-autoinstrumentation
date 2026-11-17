// SPDX-License-Identifier: Apache-2.0

package source

import (
	"regexp"
	"slices"
	"strings"
)

// The span attribute keys impact.ai withholds in Metadata only mode: keys, key prefixes and RE2 patterns. A decoder
// that keeps a source's attributes never renames one of these to a key that is not withheld.
// testdata/zipkin/intake-content.json holds the same list with impact.ai's vectors; a test keeps the two equal.
var (
	intakeContentKeys = []string{
		"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions", "gen_ai.tool.call.arguments",
		"gen_ai.tool.call.result", "gen_ai.retrieval.query.text", "gen_ai.retrieval.documents",
		"gen_ai.evaluation.explanation", "gen_ai.prompt", "gen_ai.completion", "traceloop.entity.input",
		"traceloop.entity.output", "traceloop.prompt.template", "traceloop.prompt.template_variables", "gen_ai.task.input",
		"gen_ai.task.output", "llm.content.completion.chunk", "gen_ai.content.completion.chunk", "mcp.request.argument",
		"mcp.response.value", "input.value", "output.value", "llm.prompt_template.template",
		"llm.prompt_template.variables", "llm.function_call", "reranker.query", "audio.transcript",
		"langfuse.observation.input", "langfuse.observation.output", "langfuse.trace.input", "langfuse.trace.output",
		"langfuse.observation.metadata", "langfuse.trace.metadata", "langfuse.experiment.metadata",
		"langfuse.experiment.item.metadata", "langfuse.experiment.item.expected_output", "logfire.msg", "final_result",
		"pydantic_ai.all_messages", "tool_arguments", "tool_response", "request_data", "response_data",
		"gcp.vertex.agent.llm_request", "gcp.vertex.agent.llm_response", "gcp.vertex.agent.tool_call_args",
		"gcp.vertex.agent.tool_response", "gcp.vertex.agent.data", "adk.experimental.grounding.web_search_queries",
	}
	intakeContentPrefixes = []string{
		"gen_ai.prompt.variable.", "traceloop.prompt.template_variables.", "datadog.content.", "newrelic.content.",
		"dynatrace.content.", "langsmith.content.", "langfuse.observation.metadata.", "langfuse.trace.metadata.",
	}
	intakeContentPatterns = []string{
		`^gen_ai\.(prompt|completion)\.[0-9]+\.(content|refusal)$`,
		`^gen_ai\.(prompt|completion)\.[0-9]+\.tool_calls\.[0-9]+\.arguments$`,
		`^llm\.(input|output)_messages\.[0-9]+\.message\.content$`,
		`^llm\.(input|output)_messages\.[0-9]+\.message\.contents\.[0-9]+\.message_content\.(text|data|encrypted_content)$`,
		`^llm\.(input|output)_messages\.[0-9]+\.message\.contents\.[0-9]+\.message_content\.(audio\.audio\.transcript|document\.document\.content)$`,
		`^llm\.prompts\.[0-9]+\.prompt\.text$`,
		`^llm\.choices\.[0-9]+\.completion\.text$`,
		`(^|\.)tool_call\.function\.arguments$`,
		`(^|\.)message\.function_call_arguments_json$`,
		`^(retrieval\.documents|reranker\.(input|output)_documents|input\.documents|output\.documents)\.[0-9]+\.document\.content$`,
		`^embedding\.embeddings\.[0-9]+\.embedding\.text$`,
		`(^|\.)(image|audio|video|document)\.url$`,
	}
	intakeContentExpressions = func() []*regexp.Regexp {
		out := make([]*regexp.Regexp, len(intakeContentPatterns))
		for i, p := range intakeContentPatterns {
			out[i] = regexp.MustCompile(p)
		}
		return out
	}()
)

// intakeWithholds tells whether impact.ai withholds an attribute key in Metadata only mode.
func intakeWithholds(key string) bool {
	if slices.Contains(intakeContentKeys, key) {
		return true
	}
	for _, prefix := range intakeContentPrefixes {
		if strings.HasPrefix(key, prefix) {
			return true
		}
	}
	for _, e := range intakeContentExpressions {
		if e.MatchString(key) {
			return true
		}
	}
	return false
}
