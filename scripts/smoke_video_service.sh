#!/usr/bin/env bash
# ============================================================================
# Smoke test for the FastAPI Hyperframes Video Service
# ============================================================================
# Prerequisites:
#   docker compose up -d postgres redis api
#
# Usage:
#   bash scripts/smoke_video_service.sh
# ============================================================================

set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
PROMPT="帮我用hyperframe这个skill，做一个交通银行的宣传视频。无头浏览器的地址在：/opt/chrome-headless-shell-linux64/chrome-headless-shell"

echo "=== Smoke Test: OpenHarness Video Service ==="
echo "Base URL: $BASE_URL"
echo ""

# 1. Health check
echo "--- Health Check ---"
HEALTH=$(curl -sf "$BASE_URL/healthz" || echo '{"status":"error"}')
echo "Health: $HEALTH"
echo ""

# 2. Submit a video task
echo "--- Submit Video Task ---"
RESPONSE=$(curl -sf -X POST "$BASE_URL/v1/videos" \
  -H "Content-Type: application/json" \
  -d "{\"prompt\":\"$PROMPT\"}")
echo "Response: $RESPONSE"

TASK_ID=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")
echo "Task ID: $TASK_ID"
echo ""

# 3. Poll for status (max 30 minutes)
echo "--- Poll Task Status ---"
MAX_ATTEMPTS=180
ATTEMPT=0
STATUS=""

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    STATUS_RESPONSE=$(curl -sf "$BASE_URL/v1/videos/$TASK_ID" || echo '{"status":"error"}')
    STATUS=$(echo "$STATUS_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    echo "  [$ATTEMPT/$MAX_ATTEMPTS] Status: $STATUS"

    if [ "$STATUS" = "succeeded" ] || [ "$STATUS" = "failed" ] || [ "$STATUS" = "canceled" ]; then
        break
    fi

    sleep 10
done

echo ""
echo "Final status: $STATUS"

# 4. If succeeded, download the video
if [ "$STATUS" = "succeeded" ]; then
    echo ""
    echo "--- Download Video ---"
    OUTPUT_FILE="/tmp/smoke_test_${TASK_ID}.mp4"
    HTTP_CODE=$(curl -sf -o "$OUTPUT_FILE" -w "%{http_code}" "$BASE_URL/v1/videos/$TASK_ID/file")
    echo "Download HTTP code: $HTTP_CODE"

    if [ "$HTTP_CODE" = "200" ]; then
        FILE_SIZE=$(stat -c%s "$OUTPUT_FILE" 2>/dev/null || echo "unknown")
        echo "Downloaded file size: $FILE_SIZE bytes"
        echo "File saved to: $OUTPUT_FILE"
    else
        echo "Download failed!"
    fi
else
    echo "Task did not succeed. Skipping download."
fi

echo ""
echo "=== Smoke Test Complete ==="
