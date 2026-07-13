#!/usr/bin/env bash
# End-to-end acceptance runner for the scale-multi-instance change (R7-R13).
#
# Self-contained: builds the e2e derived image, pulls infra, brings up the
# multi-replica stack (api x2 / worker x2 + beat + postgres/redis/minio), then
# exercises ownership/reclaim, cross-replica cancel, S3 302 download, MinIO
# restart degradation, and the per-worker concurrency cap.
#
# Usage:  bash e2e/run_e2e.sh
set -u

cd "$(dirname "$0")/.."            # -> OpenHarness
PROJECT="${E2E_PROJECT:-e2e}"
COMPOSE="docker compose -p $PROJECT -f docker-compose.e2e.yml"
IMAGE=oh-e2e:latest
REPORT="${E2E_REPORT:-/tmp/e2e_report.txt}"
: > "$REPORT"

pass=0; fail=0
log()  { echo "$*" | tee -a "$REPORT"; }
ok()   { log "PASS | $1"; pass=$((pass+1)); }
bad()  { log "FAIL | $1"; fail=$((fail+1)); }
# check <name> <0|nonzero>
check(){ if [ "$2" = "0" ]; then ok "$1"; else bad "$1"; fi; }

# --- container HTTP helpers (deterministic replica identity via docker exec) ---
api_get()    { docker exec "${PROJECT}-api-$1" curl -s -m 15 "localhost:8000$2"; }
api_code()   { docker exec "${PROJECT}-api-$1" curl -s -m 15 -o /dev/null -w '%{http_code}' "localhost:8000$2"; }
api_head()   { docker exec "${PROJECT}-api-$1" curl -s -m 15 -D - -o /dev/null "localhost:8000$2"; }
api_post()   { docker exec "${PROJECT}-api-$1" curl -s -m 15 -H 'Content-Type: application/json' -d "$3" "localhost:8000$2"; }
api_delete() { docker exec "${PROJECT}-api-$1" curl -s -m 15 -X DELETE "localhost:8000$2"; }

json_field(){ sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p" | head -1; }

submit_task(){ # replica prompt
  local r="$1" p="$2"
  local resp; resp=$(api_post "$r" "/v1/videos" "{\"prompt\":\"$p\"}")
  echo "$resp" | json_field task_id
}
get_status(){ api_get 1 "/v1/videos/$1" | json_field status | tr '[:upper:]' '[:lower:]'; }

wait_status(){ # task_id target timeout
  local t=0 id="$1" tgt="$2" to="$3" s
  while [ $t -lt "$to" ]; do
    s=$(get_status "$id")
    [ "$s" = "$tgt" ] && return 0
    sleep 5; t=$((t+5))
  done
  return 1
}

# ===========================================================================
log "===== scale-multi-instance e2e  $(date -u +%FT%TZ) ====="

# --- 1. Build derived image ------------------------------------------------
log "--- building $IMAGE ---"
docker build -t "$IMAGE" -f Dockerfile.e2e . 2>&1 | tail -5 || { bad "image build"; cat "$REPORT"; exit 1; }
ok "derived image built"

# --- 2. Pull infra + bring up stack ----------------------------------------
log "--- pulling infra images ---"
$COMPOSE pull postgres redis minio createbuckets 2>&1 | tail -3

log "--- bringing up stack (api x2 / worker x2) ---"
$COMPOSE up -d --scale api=2 --scale worker=2 2>&1 | tail -8

# wait for both api replicas to answer /healthz 200
for r in 1 2; do
  t=0; while [ $t -lt 150 ]; do
    c=$(api_code "$r" "/healthz" 2>/dev/null || echo 000)
    [ "$c" = "200" ] && break
    sleep 5; t=$((t+5))
  done
  [ "$c" = "200" ] && ok "api-$r responds /healthz" || bad "api-$r /healthz timeout (code=$c)"
done

# ===========================================================================
# TEST A: observability + horizontal scaling (R11 / R12)
log "--- TEST A: endpoints across replicas (R11/R12) ---"
for r in 1 2; do
  h=$(api_get "$r" "/healthz")
  echo "$h" | grep -q '"s3"' && ok "api-$r /healthz has s3 field" || bad "api-$r /healthz missing s3 field"
  rc=$(api_code "$r" "/readyz"); [ "$rc" = "200" ] && ok "api-$r /readyz 200" || bad "api-$r /readyz code=$rc"
  m=$(api_get "$r" "/metrics")
  echo "$m" | grep -q 'oh_render_inflight' && ok "api-$r /metrics exposes oh_render_inflight" || bad "api-$r /metrics missing oh_render_inflight"
done

# ===========================================================================
# TEST B: render + S3 302 download (R3 / R10 / R11)
log "--- TEST B: render -> S3 302 download (R3/R10) ---"
tid=$(submit_task 1 "e2e-render-s3")
log "  submitted task $tid"
wait_status "$tid" succeeded 240 && ok "task succeeded (R7 claim + render)" || { bad "task did not succeed (status=$(get_status $tid))"; }
hdr=$(api_head 1 "/v1/videos/$tid/file")
code=$(echo "$hdr" | head -1 | grep -o '[0-9]\{3\}')
loc=$(echo "$hdr" | grep -i '^location:' | tr -d '\r')
echo "$loc" | grep -qi 'minio:9000' && echo "$loc" | grep -qi 'openharness' \
  && { [ "$code" = "302" ] && ok "GET /file -> 302 presigned minio URL (R3/R10)" || bad "GET /file code=$code (expected 302)"; } \
  || bad "GET /file not a 302 minio presigned url (code=$code loc=$loc)"

# ===========================================================================
# TEST C: worker crash -> reclaim + takeover (R7 / R8 / R9)
log "--- TEST C: worker crash reclaim (R7/R8/R9) ---"
# submit several long tasks so both workers hold running work
cids=()
for i in 1 2 3 4; do cids+=("$(submit_task 1 "e2e-reclaim-$i")"); done
log "  submitted reclaim tasks: ${cids[*]}"
# wait until at least 3 are running
t=0; while [ $t -lt 120 ]; do
  n=0; for id in "${cids[@]}"; do [ "$(get_status "$id")" = "running" ] && n=$((n+1)); done
  [ $n -ge 3 ] && break; sleep 5; t=$((t+5))
done
log "  running before kill: $(for id in "${cids[@]}"; do get_status "$id"; done | tr '\n' ' ')"
# Kill worker-1 (SIGKILL) to simulate a hard crash. With 4 tasks spread across
# 2 workers, worker-1 owns ~2 running tasks; its crash orphans them and the
# beat's recover_lost_tasks re-enqueues them for the surviving worker. We prove
# the takeover structurally from DB final state (owner change + attempt bump),
# because get_worker_id() is per-process and cannot be mapped from a docker exec
# (a fresh `python -c get_worker_id()` yields a different uuid than the celery
# worker that actually owns the task).
owner_of() { docker exec ${PROJECT}-postgres-1 psql -U oh -d oh -t -A -c "SELECT worker_id FROM video_tasks WHERE id='$1';" 2>/dev/null; }
task_oa() { docker exec ${PROJECT}-postgres-1 psql -U oh -d oh -t -A -c "SELECT worker_id, attempt FROM video_tasks WHERE id='$1';" 2>/dev/null; }
# capture original owners before the kill (authoritative proof baseline)
declare -A owner_map
for id in "${cids[@]}"; do owner_map["$id"]=$(owner_of "$id" | tr -d '[:space:]'); done
docker kill -s SIGKILL ${PROJECT}-worker-1 && log "  killed ${PROJECT}-worker-1 (crash simulation)"
# poll all tasks; track reclaim (running -> retrying -> running -> succeeded)
saw_retry=0; all_ok=1; trail=""
for id in "${cids[@]}"; do
  seen_retry=0; t=0
  while [ $t -lt 420 ]; do
    s=$(get_status "$id")
    trail="$trail $s"
    [ "$s" = "retrying" ] && seen_retry=1
    if [ "$s" = "succeeded" ]; then [ $seen_retry -eq 1 ] && saw_retry=1; break; fi
    if [ "$s" = "failed" ] || [ "$s" = "canceled" ]; then all_ok=0; break; fi
    sleep 3; t=$((t+3))
  done
  [ "$t" -ge 420 ] && all_ok=0
done
log "  status trail:$trail"
# Authoritative reclaim proof via DB final state. The transient RETRYING state
# can be missed by the 3s sampler, so we verify structurally: a reclaimed task
# changed OWNER (orig_owner != final_owner) and had its attempt bumped by
# recover_lost_tasks (0 -> 1). Owner change + attempt bump = takeover by the
# surviving replica. The manual claim in generate_video_task does NOT increment
# attempt, so a reclaimed task ends at attempt=1 exactly.
reclaimed=0
for id in "${cids[@]}"; do
  om=$(echo "${owner_map[$id]}" | tr -d '[:space:]')
  oa=$(task_oa "$id"); ow=$(echo "$oa" | cut -d'|' -f1 | tr -d '[:space:]'); at=$(echo "$oa" | cut -d'|' -f2 | tr -d '[:space:]')
  log "  proof $id: orig_owner=$om final=$ow attempt=$at"
  if [ -n "$om" ] && [ "$om" != "$ow" ] && [ "${at:-0}" -ge 1 ]; then reclaimed=1; fi
done
echo "$trail" | grep -q retrying && saw_retry=1
[ $saw_retry -eq 1 ] && ok "reclaim observed live (running->retrying->succeeded) (R8/R9)"
[ $reclaimed -eq 1 ] && ok "reclaim+takeover proved via DB (owner change + attempt bump) (R7/R8/R9)" || bad "no reclaim transition observed (see proof lines above)"
[ $all_ok -eq 1 ] && ok "all crashed-worker tasks eventually succeeded on surviving replica (R7)" || bad "some reclaimed task did not succeed"

# ===========================================================================
# TEST D: cross-replica cancellation (R9)
log "--- TEST D: cross-replica cancel (R9) ---"
tid=$(submit_task 1 "e2e-cancel")
wait_status "$tid" running 120 && log "  task $tid running" || bad "cancel task never running (status=$(get_status $tid))"
# DELETE issued from the OTHER api replica (api-2) -> cross-replica abort flag
del=$(api_delete 2 "/v1/videos/$tid")
echo "$del" | grep -qi 'canceled' && ok "api-2 DELETE returned canceled" || bad "api-2 DELETE unexpected: $del"
wait_status "$tid" canceled 90 && ok "task canceled after cross-replica delete (R9)" || bad "task not canceled (status=$(get_status $tid))"

# ===========================================================================
# TEST E: MinIO restart -> degraded (non-fatal) health (R11)
log "--- TEST E: MinIO restart degraded health (R11) ---"
docker stop ${PROJECT}-minio-1 && log "  stopped minio"
# /healthz must stay responsive (HTTP 200, degraded) while S3 is down (R11).
# Retry a few times to ride out any single transient probe hiccup; the product
# fix caps the S3 probe at ~2s so a 200-degraded answer is expected quickly.
h=""; code=""
for try in 1 2 3 4 5; do
  sleep 2
  h=$(api_get 1 "/healthz"); code=$(api_code 1 "/healthz")
  echo "$h" | grep -q '"s3": *"error"' && [ "$code" = "200" ] && break
done
echo "$h" | grep -q '"s3": *"error"' && [ "$code" = "200" ] \
  && ok "healthz degraded (s3=false) but HTTP 200 (non-fatal) (R11)" \
  || bad "healthz after minio stop unexpected (code=$code body=$h)"
docker start ${PROJECT}-minio-1 && log "  restarted minio"
# wait for minio healthy + recreate bucket (createbuckets already ran; minio keeps volume)
t=0; while [ $t -lt 60 ]; do
  c=$(docker exec ${PROJECT}-minio-1 curl -s -m 3 -o /dev/null -w '%{http_code}' http://localhost:9000/minio/health/live 2>/dev/null || echo 000)
  [ "$c" = "200" ] && break; sleep 3; t=$((t+3))
done
sleep 3
h=$(api_get 1 "/healthz")
echo "$h" | grep -q '"s3": *"ok"' && ok "healthz s3 recovered true after minio restart (R11)" || bad "healthz s3 did not recover (body=$h)"

# ===========================================================================
# TEST F: per-worker concurrency cap (R13)
log "--- TEST F: per-worker concurrency cap (R13) ---"
log "  recreating worker with concurrency=1 / max_concurrent_renders=1"
OH_CELERY_CONCURRENCY=1 OH_MAX_RENDERS=1 $COMPOSE up -d --force-recreate --scale worker=1 --scale api=1 2>&1 | tail -3
# Guarantee a single worker: remove any extra worker containers (compose scale
# can briefly leave the old 2nd worker alive, which would let 2 renders run and
# violate the cap assertion). Keep only ${PROJECT}-worker-1.
for w in $(docker ps -a --filter "name=${PROJECT}-worker-" -q 2>/dev/null); do
  cn=$(docker inspect --format '{{.Name}}' "$w" 2>/dev/null | sed 's#^/##')
  [ "$cn" = "${PROJECT}-worker-1" ] || docker rm -f "$w" 2>/dev/null
done
# Wait until exactly ONE worker container is Up. The --scale worker=1 recreate
# can briefly leave the old 2nd worker alive, which would let 2 renders run
# concurrently and violate the cap assertion. Submit only once it has settled.
wt=0
while [ $wt -lt 60 ]; do
  n=$(docker ps -a --filter "name=${PROJECT}-worker-" --filter "status=running" -q 2>/dev/null | wc -l)
  [ "$n" -eq 1 ] && break
  sleep 3; wt=$((wt+3))
done
log "  worker containers Up after settle: $(docker ps -a --filter "name=${PROJECT}-worker-" --filter "status=running" -q 2>/dev/null | wc -l)"
# give the lone worker a moment to import and arm its (capped) semaphore
sleep 5
fids=()
for i in 1 2 3; do fids+=("$(submit_task 1 "e2e-cap-$i")"); done
log "  submitted cap tasks: ${fids[*]}"
# The semaphore caps CONCURRENT RENDER PROCESSES, not DB "running" rows and not
# Celery prefetch. The oh-stub spends most of its life in `sleep`, so counting
# concurrent `sleep` processes in the worker container is the authoritative
# measure of the per-worker concurrency cap.
max_concurrent=0; all_ok=1
t=0
while [ $t -lt 420 ]; do
  n=$(docker exec ${PROJECT}-worker-1 sh -c 'ps -e -o comm= 2>/dev/null | grep -c "^sleep$"' 2>/dev/null || echo 0)
  [ "$n" -gt "$max_concurrent" ] && max_concurrent=$n
  done=0
  for id in "${fids[@]}"; do
    s=$(get_status "$id")
    [ "$s" = "succeeded" ] && done=$((done+1))
    [ "$s" = "failed" ] && all_ok=0
  done
  [ $done -eq 3 ] && break
  sleep 2; t=$((t+2))
done
[ $max_concurrent -le 1 ] && ok "concurrent stub renders never exceeded 1 (max_observed=$max_concurrent) (R13)" || bad "concurrency cap violated (max_observed=$max_concurrent)"
[ $all_ok -eq 1 ] && ok "all capped tasks succeeded (R13)" || bad "a capped task failed"

# ===========================================================================
log "===== SUMMARY: pass=$pass fail=$fail ====="
cat "$REPORT"
# teardown
log "--- tearing down stack ---"
$COMPOSE down -v 2>&1 | tail -3
exit $fail
