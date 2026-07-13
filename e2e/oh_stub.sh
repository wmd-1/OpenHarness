#!/usr/bin/env bash
# Faithful stand-in for the `oh` CLI, used ONLY in the multi-instance e2e suite.
#
# It mirrors the real invocation shape exactly:
#   oh -p <prompt> --output-format text --permission-mode full_auto [extra_args]
#
# and behaves like a real render for the purposes of exercising the
# scale-multi-instance orchestration (R7-R13):
#   * renders a tiny but VALID .mp4 into the cwd (so parser.py / probe_mp4 work)
#   * prints the "**Output:** `out.mp4`" marker that parser.py looks for
#   * honours a configurable sleep (OH_STUB_SLEEP_SECONDS) so reclaim / cancel
#     scenarios have a window in which to act
#   * exits on SIGTERM -- run_oh tears down the whole process group with SIGTERM
#     when a task is canceled or times out, so this must honour it
set -u

SLEEP_SECONDS="${OH_STUB_SLEEP_SECONDS:-90}"
OUT_NAME="out.mp4"

# run_oh kills the process group (setsid'd) with SIGTERM on cancel/timeout.
trap 'echo "[oh-stub] SIGTERM received -> aborting render"; exit 143' TERM

echo "[oh-stub] render start pid=$$ pgid=$$"
if command -v ffmpeg >/dev/null 2>&1; then
  # 1-second solid-blue 320x240 mp4 -- a real, ffprobe-readable artifact.
  ffmpeg -y -f lavfi -i color=c=blue:s=320x240:d=1 -pix_fmt yuv420p "$OUT_NAME" >/dev/null 2>&1 \
    || : > "$OUT_NAME"
else
  : > "$OUT_NAME"
fi

echo "[oh-stub] wrote $OUT_NAME; simulating render for ${SLEEP_SECONDS}s"
sleep "$SLEEP_SECONDS"

# Marker consumed by app.workers.parser.locate_output_file
echo "**Output:** \`$OUT_NAME\`"
echo "[oh-stub] render complete"
exit 0
