#!/bin/bash
# U6 live proof: one omp run with AOS_OPENSHELL_COMMS=1 on a disposable bus (127.0.0.1:8017), a scratch aos-example
# clone and the image aos-harness:u6. The desk answers as the operator through the bearer route. Secrets never printed.
set -u
W=/Users/operator/.aos/worktrees/agent-os/agent-comms-52
L=<scratch>
PORT=8017
BIN=$W/.venv/bin
log() { echo "$(date '+%H:%M:%S') $*" >> $L/logs/driver.log; }
umask 077
[ -f $L/bus-token ] || python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > $L/bus-token
TOKEN=$(cat $L/bus-token)
NOTE_WORD=NOTE-$(python3 -c 'import secrets;print(secrets.token_hex(4))'); FRUIT_WORD=FRUIT-$(python3 -c 'import secrets;print(secrets.token_hex(4))')
printf '{"note": "%s", "fruit": "%s"}\n' $NOTE_WORD $FRUIT_WORD > $L/words.json
BASE=(PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin HOME=$HOME LANG=C.UTF-8)
mkdir -p $L/bus-data $L/openshell-state $L/repos
[ -d $L/repos/aos-example ] || { git clone -q --no-hardlinks $HOME/code/aos-example $L/repos/aos-example && git -C $L/repos/aos-example remote remove origin; }
log "repo at $(git -C $L/repos/aos-example rev-parse --short HEAD)"
openshell sandbox list > $L/logs/sandbox-list-before.txt 2>&1
cd $W
env -i "${BASE[@]}" BUS_TOKEN=$TOKEN BUS_DATA_DIR=$L/bus-data TASK_SWEEP_INTERVAL_SECONDS=10 \
  $BIN/python -m uvicorn agent_os.api:create_app --factory --host 127.0.0.1 --port $PORT >> $L/logs/bus.log 2>&1 &
BUS_PID=$!; echo $BUS_PID > $L/bus.pid
for i in $(seq 1 60); do curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$PORT/health 2>/dev/null | grep -q 200 && break; sleep 1; done
log "bus pid $BUS_PID on $PORT"
CLI=(env -i "${BASE[@]}" AOS_URL=http://127.0.0.1:$PORT AOS_TOKEN=$TOKEN)
"${CLI[@]}" $BIN/aos add --repo aos-example --title "U6 omp talks to the operator" --brief-file $L/briefs/omp.md \
  --accept "grep -q '^Fruit: FRUIT-' FRUIT.md" --accept "grep -q '^Note: NOTE-' FRUIT.md" \
  --harness pi --host mac > $L/logs/add.json 2> $L/logs/add.err
ID=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["id"])' $L/logs/add.json)
log "task $ID"
# the unsolicited operator note, stored before the run starts (the agent reads it through mcp__aos_inbox)
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://127.0.0.1:$PORT/tasks/operator/intents \
  -d "{\"to_task_id\": \"$ID\", \"body\": \"Also write the line 'Note: $NOTE_WORD' into FRUIT.md.\"}" > $L/logs/note.json
log "note $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["id"])' $L/logs/note.json)"
"${CLI[@]}" $BIN/aos ready $ID >> $L/logs/add.err 2>&1
# the operator's answer: poll the task's intents, answer the first operator-addressed request once
( for i in $(seq 1 360); do
    q=$(curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:$PORT/tasks/$ID/intents | python3 -c '
import json,sys
rows=json.load(sys.stdin)
q=[r for r in rows if r.get("to_task_id")=="operator" and r.get("kind")=="request" and r.get("state") in ("stored","delivered")]
print(q[0]["id"] if q else "")')
    if [ -n "$q" ]; then
      log "question $q seen"
      curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -X POST http://127.0.0.1:$PORT/tasks/operator/intents \
        -d "{\"reply_to\": \"$q\", \"body\": \"$FRUIT_WORD\"}" > $L/logs/answer.json
      log "answered $q"; break
    fi
    sleep 5
  done ) &
ANSWERER=$!
log "run $ID"
env -i "${BASE[@]}" AOS_URL=http://127.0.0.1:$PORT AOS_TOKEN=$TOKEN AOS_HOST=mac AOS_REPO_ROOT=$L/repos \
  AOS_OPENSHELL_PLATFORM=colima-mac AOS_OPENSHELL_IMAGE=aos-harness:u6 AOS_OPENSHELL_BUS_PORT=$PORT \
  AOS_OPENSHELL_STATE_DIR=$L/openshell-state AOS_HARNESS_TIMEOUT_SECONDS=1800 AOS_OPENSHELL_COMMS=1 \
  AOS_OPENSHELL_PI_AUTH=$HOME/.aos/worker/pi-auth.json AOS_OPENSHELL_PI_FLAVOUR=omp \
  AOS_OPENSHELL_PI_MODEL=openai-codex/gpt-6-luna AOS_OPENSHELL_HARNESS_HOSTS_PI=chatgpt.com:443 PYTHONUNBUFFERED=1 \
  $BIN/aos run --backend openshell $ID > $L/logs/run.log 2>&1
log "run exit $?"
kill $ANSWERER 2>/dev/null
"${CLI[@]}" $BIN/aos show $ID > $L/logs/show.txt 2>&1 || true
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:$PORT/tasks/$ID/intents > $L/logs/intents.json
openshell sandbox list > $L/logs/sandbox-list-after.txt 2>&1
kill $BUS_PID; log "bus stopped"; log "done"
