This is a mechanism probe, not a feature task: write /sandbox/repo/stopguard-ran.txt, then finish.

Write it with one Bash call, exactly: printf 'stopguard-ran\n' > /sandbox/repo/stopguard-ran.txt

Then end your turn with a one-line final message. If a hook blocks your stop, do not edit any
file and do not start any other work: reply in one line that a stop hook blocked you, quoting the
first sentence of its reason, and end your turn again.
