#!/usr/bin/env bash
# Phase 3 (WS-A multi-tenancy + WS-B Temporal + WS-C strict lease/fencing)
# end-to-end acceptance runner.
#
# Builds a dedicated Phase 3 e2e image via Dockerfile.e2e.phase3, which is
# built DIRECTLY FROM the real OpenHarness base image (NOT from the
# scale-multi-instance `oh-e2e:latest` intermediate image), and exercises the
# two integration scenarios the unit suite cannot cover without Docker:
#   1. cross-tenant isolation (X-API-Key auth + 404 on cross-tenant reads)
#   2. strict lease / fencing under a real worker crash + reclaim
#      (no duplicate valid artifact; the fence table records exactly one
#       winning token)
#
# The same assertions are run under BOTH scheduler backends ("double run"):
#   * celery   - full crash+fencing (kill a worker, beat reclaims, a peer
#                re-claims the task and finishes it with a fenced token)
#   * temporal  - isolation + happy-path fencing. The crash+fencing path under
#                 Temporal is blocked by a known reclaim gap (see README sec. 3);
#                 the happy path still proves the fence wiring records exactly
#                 one winning token under the Temporal backend.
#
# Usage:  bash e2e/run_e2e_phase3.sh
set -u

cd "$(dirname "$0")/.."            # -> OpenHarness
PROJECT="${E2E_PROJECT:-e2ephase3}"
COMPOSE="docker compose -p $PROJECT -f docker-compose.e2e.phase3.yml"
IMAGE=oh-e2e-phase3:latest
REPORT="${E2E_REPORT:-/tmp/e2e_phase3_report.txt}"
: > "$REPORT"

# MUST match service/e2e_seed.py
ALPHA_KEY="alpha-secret-key"
BETA_KEY="beta-secret-key"

pass=0; fail=0
log()  { echo "$*" | tee -a "$REPORT"; }
ok()   { log "PASS | $1"; pass=$((pass+1)); }
bad()  { log "FAIL | $1"; fail=$((fail+1)); }
check(){ if [ "$2" = "0" ]; then ok "$1"; else bad "$1"; fi; }

# --- HTTP / DB helpers (executed inside the api1 container) ----------------
api_curl() { # method path [body] [extra-headers]
  local m="$1" p="$2" b="${3:-}" h="${4:-}"
  local args=(-s -m 20 -X "$m" "http://localhost:8000$p")
  [ -n "$h" ] && args+=($h)
  [ -n "$b" ] && args+=(-H 'Content-Type: application/json' -d "$b")
  docker exec "${PROJECT}-api1" curl "${args[@]}"
}
api_code() { # method path [body] [extra-headers]
  local m="$1" p="$2" b="${3:-}" h="${4:-}"
  local args=(-s -m 20 -o /dev/null -w '%{http_code}' -X "$m" "http://localhost:8000$p")
  [ -n "$h" ] && args+=($h)
  [ -n "$b" ] && args+=(-H 'Content-Type: application/json' -d "$b")
  docker exec "${PROJECT}-api1" curl "${args[@]}"
}
db_query() { # sql -> first column of first row (trimmed)
  docker exec "${PROJECT}-postgres" psql -U oh -d oh -tAc "$1" 2>/dev/null | head -1 | tr -d '[:space:]'
}
json_field(){ sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p" | head -1; }

submit()     { api_curl POST /v1/videos "{\"prompt\":\"$2\"}" "-H X-API-Key:$1" | json_field task_id; }
task_status(){ db_query "SELECT status FROM video_tasks WHERE id='$1';"; }
task_worker(){ db_query "SELECT worker_id FROM video_tasks WHERE id='$1';"; }
task_token() { db_query "SELECT lease_token FROM video_tasks WHERE id='$1';"; }

wait_db_status() { # task_id target timeout
  local id="$1" tgt="$2" to="$3" t=0 s
  while [ $t -lt "$to" ]; do
    s=$(task_status "$id"); [ "$s" = "$tgt" ] && return 0
    sleep 5; t=$((t+5))
  done
  return 1
}
wait_claimed() { # task_id timeout -> sets WORKER
  local id="$1" to="$2" t=0 w tok
  while [ $t -lt "$to" ]; do
    w=$(task_worker "$id"); tok=$(task_token "$id")
    if [ -n "$w" ] && [ "$tok" != "0" ] && [ "$(task_status "$id")" = "running" ]; then
      WORKER="$w"; return 0
    fi
    sleep 5; t=$((t+5))
  done
  return 1
}
wait_health() { # timeout
  local t=0
  while [ $t -lt "$1" ]; do
    [ "$(api_code GET /healthz)" = "200" ] && return 0
    sleep 3; t=$((t+3))
  done
  return 1
}

# --- assertions ------------------------------------------------------------
assert_isolation() {
  log "--- cross-tenant isolation ---"
  local TA TB c
  TA=$(submit "$ALPHA_KEY" "alpha isolation task")
  [ -z "$TA" ] && { bad "isolation: alpha submit returned no task_id"; return 1; }
  TB=$(submit "$BETA_KEY" "beta isolation task")
  [ -z "$TB" ] && { bad "isolation: beta submit returned no task_id"; return 1; }

  c=$(api_code GET "/v1/videos/$TA" "" "-H X-API-Key:$ALPHA_KEY"); check "alpha reads own task (200)"       "$([ "$c" = 200 ] && echo 0 || echo 1)"
  c=$(api_code GET "/v1/videos/$TA" "" "-H X-API-Key:$BETA_KEY");  check "beta cannot read alpha task (404)" "$([ "$c" = 404 ] && echo 0 || echo 1)"
  c=$(api_code GET "/v1/videos/$TB" "" "-H X-API-Key:$BETA_KEY");  check "beta reads own task (200)"         "$([ "$c" = 200 ] && echo 0 || echo 1)"
  c=$(api_code GET "/v1/videos/$TB" "" "-H X-API-Key:$ALPHA_KEY"); check "alpha cannot read beta task (404)" "$([ "$c" = 404 ] && echo 0 || echo 1)"
  c=$(api_code GET "/v1/videos/$TA");                              check "missing key rejected (401)"        "$([ "$c" = 401 ] && echo 0 || echo 1)"
  c=$(api_code GET /healthz);                                     check "healthz open without key (200)"    "$([ "$c" = 200 ] && echo 0 || echo 1)"
}

# Celery only: kill the owning worker, let beat reclaim + a peer re-claim.
assert_fencing_crash() {
  log "--- lease fencing / reclaim (celery) ---"
  local TA fence_cnt ctok fTok fKey out
  TA=$(submit "$ALPHA_KEY" "fencing-celery task")
  [ -z "$TA" ] && { bad "fencing: submit returned no task_id"; return 1; }
  wait_claimed "$TA" 120 || { bad "fencing: task never claimed"; return 1; }
  log "fencing: task $TA claimed by $WORKER (token $(task_token "$TA"))"
  docker kill "${PROJECT}-$WORKER" >/dev/null 2>&1
  log "fencing: killed $WORKER; awaiting beat reclaim + peer re-claim"
  wait_db_status "$TA" succeeded 300 || { bad "fencing: task never succeeded after reclaim"; return 1; }
  ok "fencing: task succeeded after worker crash + reclaim (celery)"

  fence_cnt=$(db_query "SELECT count(*) FROM video_lease_fence WHERE task_id='$TA';")
  ctok=$(task_token "$TA")
  fTok=$(db_query "SELECT accepted_token FROM video_lease_fence WHERE task_id='$TA';")
  fKey=$(db_query "SELECT storage_key FROM video_lease_fence WHERE task_id='$TA';")
  out=$(db_query "SELECT output_path FROM video_tasks WHERE id='$TA';")
  check "fence table has exactly one accepted artifact" "$([ "$fence_cnt" = "1" ] && echo 0 || echo 1)"
  check "fence accepted_token == task.lease_token"       "$([ "$fTok" = "$ctok" ] && echo 0 || echo 1)"
  check "fence storage_key == task.output_path"          "$([ -n "$out" ] && [ "$fKey" = "$out" ] && echo 0 || echo 1)"
}

# Temporal (and the celery happy-path equivalent): no crash; assert the fence
# wiring still records exactly one winning token on success.
assert_fencing_happy() { # backend label
  local backend="$1"
  log "--- lease fencing (happy path, $backend) ---"
  local TA fence_cnt ctok fTok fKey out
  TA=$(submit "$ALPHA_KEY" "fencing-$backend task")
  [ -z "$TA" ] && { bad "fencing: submit returned no task_id"; return 1; }
  wait_db_status "$TA" succeeded 300 || { bad "fencing: task never succeeded ($backend)"; return 1; }
  ok "fencing: task succeeded ($backend)"

  fence_cnt=$(db_query "SELECT count(*) FROM video_lease_fence WHERE task_id='$TA';")
  ctok=$(task_token "$TA")
  fTok=$(db_query "SELECT accepted_token FROM video_lease_fence WHERE task_id='$TA';")
  fKey=$(db_query "SELECT storage_key FROM video_lease_fence WHERE task_id='$TA';")
  out=$(db_query "SELECT output_path FROM video_tasks WHERE id='$TA';")
  check "fence table has exactly one accepted artifact" "$([ "$fence_cnt" = "1" ] && echo 0 || echo 1)"
  check "fence accepted_token == task.lease_token"       "$([ "$fTok" = "$ctok" ] && echo 0 || echo 1)"
  check "fence storage_key == task.output_path"          "$([ -n "$out" ] && [ "$fKey" = "$out" ] && echo 0 || echo 1)"
}

# --- orchestration ---------------------------------------------------------
up_infra() {
  $COMPOSE up -d postgres redis minio createbuckets migrate seed 2>&1 | tail -3
  docker wait "${PROJECT}-migrate" >/dev/null 2>&1; local rc1=$?
  docker wait "${PROJECT}-seed"    >/dev/null 2>&1; local rc2=$?
  [ "$rc1" = "0" ] && [ "$rc2" = "0" ] || { bad "migrate/seed failed (rc=$rc1,$rc2)"; exit 1; }
}

run_backend() { # backend
  local backend="$1"
  export OH_SCHEDULER_BACKEND="$backend"
  log "===== Phase 3 e2e - $backend backend ====="
  up_infra
  if [ "$backend" = "temporal" ]; then
    $COMPOSE up -d temporal 2>&1 | tail -2
    local t=0
    while [ $t -lt 120 ]; do
      docker exec "${PROJECT}-temporal" temporal operator namespace list >/dev/null 2>&1 && break
      sleep 3; t=$((t+3))
    done
    $COMPOSE up -d api1 api2 temporal-worker 2>&1 | tail -3
  else
    $COMPOSE up -d api1 api2 worker1 worker2 beat 2>&1 | tail -3
  fi
  wait_health 180 || { bad "$backend: api never healthy"; $COMPOSE down >/dev/null 2>&1; return 1; }
  sleep 2
  assert_isolation
  if [ "$backend" = "temporal" ]; then
    assert_fencing_happy temporal
  else
    assert_fencing_crash
  fi
  $COMPOSE down >/dev/null 2>&1
}

# ===========================================================================
log "===== Phase 3 e2e ($(date -u +%FT%TZ)) - image $IMAGE ====="
docker build -t "$IMAGE" -f Dockerfile.e2e.phase3 . 2>&1 | tail -5 || { bad "image build"; exit 1; }
ok "Phase 3 e2e image built ($IMAGE)"
run_backend celery
run_backend temporal

log "===== SUMMARY: pass=$pass fail=$fail ====="
[ "$fail" = "0" ] && exit 0 || exit 1
