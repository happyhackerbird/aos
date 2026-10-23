This task checks that you can talk to the operator from inside your sandbox.

1. Call aos_inbox (a tool of the MCP server "aos"). If a message from the operator is there, follow it.
2. Ask the operator with aos_ask(to="operator", text="Which fruit should FRUIT.md name? Answer with one word.").
3. Wait for the answer with aos_wait(id) using the request id you got. If it returns without an answer, call it again, up to five times.
4. Write the file FRUIT.md with the line `Fruit: <the operator's answer, exactly as sent>`, plus any line the inbox message asked for.
5. Call aos_inbox once more, then commit FRUIT.md on the current branch.

In your final message, list the request id, the answer you received and the inbox messages you read.
