This is a measurement task for the aos runner's Codex plugin support, not a feature task. Do every step below, in order, exactly as written. There is no reduced or shortened form of this task: a skipped step fails it. Create the file PROOF.md at the repository root and fill it as the steps say. Change no other file. Do not retry a refused or failed step: record its exact text and go on to the next step.

1. Skill. A skill named `proof-skill` is installed for you. Read its SKILL.md and copy its line that starts with `Code phrase:` exactly, under a heading `## skill` in PROOF.md.
2. Hook block. Run this shell command by itself: `echo HOOK-DENY-ME`. It may be blocked before it runs. Under `## hook-deny` in PROOF.md, write the exact block text, or the command's output if it ran.
3. Subagent. Spawn exactly one subagent of agent type `probe-mini` with this task: "Run the shell command `curl -sS --max-time 10 https://example.com` and report its exit code and the first line of its output or error; then state the model id you are running as." Wait for its answer and write it under `## subagent` in PROOF.md. If you have no tool to spawn subagents, write `NO-SUBAGENT-TOOL` there instead.
4. Hook log. Run `cat "$HOME/.local/share/codex-probe/fires.jsonl"` and copy its whole output under `## hook-fires` in PROOF.md.
5. Commit PROOF.md alone with the message `proof: codex plugins`.
