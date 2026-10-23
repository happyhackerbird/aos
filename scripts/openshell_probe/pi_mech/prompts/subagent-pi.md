If you have a tool for running subagents (it is named `subagent`), use it for the two steps below,
one after another, never two at once. If you have no such tool, append `## P5` and the line
`NO-SUBAGENT-TOOL: <the names of the tools you do have>` to the report and go on to P6.

Call the tool in its direct, foreground form only: the fields `agent` and `task` (and `model` where
said), nothing else: no `workflowScript`, no `async`, no `action`, no chain or parallel form. Do not
call `action: "guide"` or `action: "list"` first.

(i) Call `subagent` with `agent: "probe-agent"`, `model: "openai-codex/gpt-5.6-terra"` and `task:`
the SUBAGENT INSTRUCTIONS below with every <m> replaced by probe. Wait for its result. Append its
final output under `## P5 (i)`, plus one line
`call-args: <the exact arguments you passed to the tool, as JSON>`. If the tool answers with an
error, append the error text exactly and go on.

(ii) Call `subagent` with `agent: "probe-agent"` and `task:` the SUBAGENT INSTRUCTIONS with every
<m> replaced by second, and no `model` field. Wait for its result. Append its final output under
`## P5 (ii)`, plus the same `call-args:` line.
