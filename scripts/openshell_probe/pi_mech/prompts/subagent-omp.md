If you have a tool for running task agents (it is named `task`), first append under `## P5` one
line `agents: <the names of the agents the task tool offers, comma-separated>`. Then use it for the
two steps below, one after another, never two at once. If you have no such tool, append `## P5` and
the line `NO-SUBAGENT-TOOL: <the names of the tools you do have>` to the report and go on to P6.

(i) Run the agent `probe-agent` with the SUBAGENT INSTRUCTIONS below as its task, with every <m>
replaced by probe, as an isolated task if the tool offers isolation (an `isolated` field on the
item or the call). Wait for its result. Append its final output under `## P5 (i)`, plus one line
`call-args: <the exact arguments you passed to the tool, as JSON>`.

(ii) Run the agent `probe-agent-home` if the tool offers it, otherwise `probe-agent` again, with
the SUBAGENT INSTRUCTIONS and every <m> replaced by second, not isolated. Wait for its result.
Append its final output under `## P5 (ii)`, plus the same `call-args:` line.
