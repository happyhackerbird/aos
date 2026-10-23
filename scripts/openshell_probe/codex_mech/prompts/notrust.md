This is a mechanism probe, not a feature task. Do each step below in order, exactly as written.
Do not retry a refused or failed step: record the refusal or error text exactly and go on. Do not
push. Change no file in the repository other than probe-report.md.

## K2: hooks

Run each of the following as its own shell command, in this order.

K2a:

cd /sandbox/repo && { echo '## K2a'; echo '@@ allowed'; echo HOOK-ALLOWED-OK; } 2>&1 | tee -a probe-report.md

K2b (it may be denied before it runs; if so, record the denial text exactly as K2b in the report with one more command, and do not retry):

cd /sandbox/repo && { echo '## K2b'; echo '@@ deny'; echo HOOK-DENY-ME; } 2>&1 | tee -a probe-report.md

K2c:

cd /sandbox/repo && { echo '## K2c'; echo '@@ firelog'; cat "$HOME/.local/share/codex-probe/fires.jsonl" 2>&1 | cut -c1-200; echo "rc=$?"; } 2>&1 | tee -a probe-report.md

## End

Run this as one shell command, exactly:

cd /sandbox/repo && git add probe-report.md && git -c user.name=codex-probe -c user.email=probe@sandbox.invalid commit -q -m "probe: codex mechanism report (notrust)" && git log --oneline -1

Then finish with a short final message whose last line is exactly `FINAL: @CODEWORD@`.
