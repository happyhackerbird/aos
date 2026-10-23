set -u
mkdir -p /tmp/claude-home /tmp/pi-agent /tmp/pi-home /sandbox/codex-home /sandbox/incoming /sandbox/pi-sessions
export CLAUDE_CONFIG_DIR=/tmp/claude-home PI_CODING_AGENT_DIR=/tmp/pi-agent CODEX_HOME=/sandbox/codex-home
aos_deadline=$(( $(date +%s) + 4200 ))
while [ ! -f /sandbox/job.tgz ]; do [ "$(date +%s)" -lt "$aos_deadline" ] || exit 1; sleep 1; done
if [ -f /sandbox/services.env ]; then . /sandbox/services.env; fi
mkdir -p /sandbox/repo && tar xzf /sandbox/job.tgz -C /sandbox/repo
cd /sandbox/repo
aos_harness() {
  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=30 2700 bash -c 'PI_CODING_AGENT_DIR=/tmp/pi-agent HOME=/tmp/pi-home omp --print --mode json --model openai-codex/gpt-6-luna --session-dir /sandbox/pi-sessions --no-extensions --no-skills --no-rules --no-title --max-time 2700 --approval-mode=yolo --tools=read,bash,edit,eval,glob,grep,wait,todo,write,web_search'; return $?
  fi
  bash -c 'PI_CODING_AGENT_DIR=/tmp/pi-agent HOME=/tmp/pi-home omp --print --mode json --model openai-codex/gpt-6-luna --session-dir /sandbox/pi-sessions --no-extensions --no-skills --no-rules --no-title --max-time 2700 --approval-mode=yolo --tools=read,bash,edit,eval,glob,grep,wait,todo,write,web_search' <&0 & aos_hp=$!
  ( sleep 2700; kill -TERM "$aos_hp" 2>/dev/null; sleep 30; kill -KILL "$aos_hp" 2>/dev/null ) </dev/null >/dev/null 2>&1 & aos_wp=$!
  wait "$aos_hp"; aos_rc=$?; kill "$aos_wp" 2>/dev/null; return $aos_rc
}
date +%s%N > /sandbox/harness.started
aos_harness </sandbox/prompt.txt >/sandbox/harness.out 2>/sandbox/harness.err
rc=$?; for p in $(pgrep -u "$(id -u)"); do case "$p" in 1|$$|$PPID) ;; *) kill "$p" 2>/dev/null || true;; esac; done; sleep 1; for p in $(pgrep -u "$(id -u)"); do case "$p" in 1|$$|$PPID) ;; *) kill -9 "$p" 2>/dev/null || true;; esac; done; echo $rc > /sandbox/harness.rc
sleep 4200
