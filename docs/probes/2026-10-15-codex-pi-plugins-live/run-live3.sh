#!/bin/bash
# Live proof of AOS_OPENSHELL_CODEX_PLUGINS and AOS_OPENSHELL_PI_PLUGINS: one aos run per harness on a
# scratch aos-example clone, against a disposable bus on 127.0.0.1:8016. Token and credentials never printed.
set -u
W=/Users/operator/.aos/worktrees/agent-os/codex-pi-mech
S=/private/tmp/claude-501/-Users-operator-code-agent-os/e9810059-ef4c-45ec-a705-ea2f7abeeff1/scratchpad/live
PORT=8016
BIN=$W/.venv/bin
log() { echo "$(date '+%H:%M:%S') $*" >> $S/logs/driver3.log; }
umask 077
[ -f $S/bus-token ] || python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > $S/bus-token
TOKEN=$(cat $S/bus-token)
BASE=(PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin HOME=$HOME LANG=C.UTF-8)
mkdir -p $S/bus-data $S/openshell-state
openshell sandbox list > $S/logs/sandbox-list-before3.txt 2>&1
cd $W
env -i "${BASE[@]}" BUS_TOKEN=$TOKEN BUS_DATA_DIR=$S/bus-data TASK_LEASE_SECONDS=120 TASK_SWEEP_INTERVAL_SECONDS=15 \
  $BIN/python -m uvicorn agent_os.api:create_app --factory --host 127.0.0.1 --port $PORT >> $S/logs/bus.log 2>&1 &
BUS_PID=$!
echo $BUS_PID > $S/bus.pid
for i in $(seq 1 60); do curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/health 2>/dev/null | grep -q 200 && break; sleep 1; done
log "bus pid $BUS_PID on $PORT"
CLI=(env -i "${BASE[@]}" AOS_URL=http://127.0.0.1:$PORT AOS_TOKEN=$TOKEN)
RUN=("${CLI[@]}" AOS_HOST=mac AOS_REPO_ROOT=$S/repos AOS_OPENSHELL_PLATFORM=colima-mac AOS_OPENSHELL_IMAGE=aos-harness:comms
     AOS_OPENSHELL_BUS_PORT=$PORT AOS_OPENSHELL_STATE_DIR=$S/openshell-state AOS_HARNESS_TIMEOUT_SECONDS=1800)
add() {  # name harness brief accept...
  local name=$1 harness=$2 brief=$3; shift 3
  local accepts=(); for a in "$@"; do accepts+=(--accept "$a"); done
  "${CLI[@]}" $BIN/aos add --repo aos-example --title "plugins live3 $name" --brief-file $brief "${accepts[@]}" \
      --harness $harness --host mac > $S/logs/add3-$name.json 2> $S/logs/add3-$name.err
  local id; id=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["id"])' $S/logs/add3-$name.json)
  "${CLI[@]}" $BIN/aos ready $id >> $S/logs/add3-$name.err 2>&1
  echo $id
}
OMP_ID=$(add omp-ext pi $S/briefs/pi2.md "grep -q 'PROBE-ECHO-OK hello-from-the-desk' PROOF.md" \
  "grep -q '^Code phrase: PHRASE-PI-' PROOF.md" "grep -q '\"event\":\"session_start\"' PROOF.md" \
  "grep -q 'NO-SUBAGENT-TOOL' PROOF.md")
log "tasks omp-ext=$OMP_ID"
echo "{\"omp-ext\": \"$OMP_ID\"}" > $S/tasks3.json
log "run omp-ext $OMP_ID"
"${RUN[@]}" AOS_OPENSHELL_PI_AUTH=$HOME/.aos/worker/pi-auth.json AOS_OPENSHELL_PI_FLAVOUR=omp \
  AOS_OPENSHELL_PI_MODEL=openai-codex/gpt-6-luna AOS_OPENSHELL_HARNESS_HOSTS_PI=chatgpt.com:443 \
  AOS_OPENSHELL_PI_PLUGINS=$S/plugins/pi-ext \
  $BIN/aos run --backend openshell $OMP_ID > $S/logs/run3-omp-ext.log 2>&1
log "omp-ext exit $?"
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:$PORT/tasks/$OMP_ID > $S/logs/task-$OMP_ID.json
for id in $OMP_ID; do "${CLI[@]}" $BIN/aos show $id > $S/logs/show3-$id.txt 2>&1 || true; done
openshell sandbox list > $S/logs/sandbox-list-after3.txt 2>&1
kill $BUS_PID; log "bus stopped"; log "done"
