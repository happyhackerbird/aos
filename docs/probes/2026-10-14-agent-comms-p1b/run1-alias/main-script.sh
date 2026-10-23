set -u
mkdir -p /tmp/claude-home /sandbox/incoming
export CLAUDE_CONFIG_DIR=/tmp/claude-home
aos_deadline=$(( $(date +%s) + 2400 ))
while [ ! -f /sandbox/job.tgz ]; do [ "$(date +%s)" -lt "$aos_deadline" ] || exit 1; sleep 1; done
mkdir -p /sandbox/repo && tar xzf /sandbox/job.tgz -C /sandbox/repo
cd /sandbox/repo
aos_harness() {
  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=30 1500 bash -c 'claude -p "$(cat /sandbox/prompt.txt)" --output-format stream-json --verbose --include-hook-events --setting-sources "" --settings /sandbox/claude-settings.json --mcp-config /sandbox/aos-mcp.json --strict-mcp-config --model sonnet --max-budget-usd 2'; return $?
  fi
  bash -c 'claude -p "$(cat /sandbox/prompt.txt)" --output-format stream-json --verbose --include-hook-events --setting-sources "" --settings /sandbox/claude-settings.json --mcp-config /sandbox/aos-mcp.json --strict-mcp-config --model sonnet --max-budget-usd 2' <&0 & aos_hp=$!
  ( sleep 1500; kill -TERM "$aos_hp" 2>/dev/null; sleep 30; kill -KILL "$aos_hp" 2>/dev/null ) </dev/null >/dev/null 2>&1 & aos_wp=$!
  wait "$aos_hp"; aos_rc=$?; kill "$aos_wp" 2>/dev/null; return $aos_rc
}
date +%s%N > /sandbox/harness.started
aos_harness </dev/null >/sandbox/harness.out 2>/sandbox/harness.err
rc=$?; for p in $(pgrep -u "$(id -u)"); do case "$p" in 1|$$|$PPID) ;; *) kill "$p" 2>/dev/null || true;; esac; done; sleep 1; for p in $(pgrep -u "$(id -u)"); do case "$p" in 1|$$|$PPID) ;; *) kill -9 "$p" 2>/dev/null || true;; esac; done; echo $rc > /sandbox/harness.rc
sleep 2400
