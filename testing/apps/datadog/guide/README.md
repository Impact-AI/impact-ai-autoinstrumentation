# The Datadog guide's commands

The guide "You already use Datadog" shows these files as they are. The harness runs each of them against its cluster,
so a command the guide shows is a command that works. Change them here and copy them to the guide's samples unchanged
but for the license line.

| File | What it does |
| --- | --- |
| `check.sh` | Prints the Agent address each pod of a workload uses (`DD_TRACE_AGENT_URL`, `DD_AGENT_HOST`). |
| `llm-observability.env` | Datadog's settings that turn LLM Observability on, which raises a workload's fidelity. |
| `undo.sh` | Replaces a workload's pods, after Stop copying, so that they use their original Agent address now. |
