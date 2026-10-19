#!/bin/bash
# Live proof for glasses-platform: a real Hermes gateway + real models (z.ai GLM coding plan: glm-5.3 main,
# glm-4.6v auxiliary vision) + the bundled simulator, graded by live_checks.py. Fully isolated from any Hermes you run:
# throwaway HERMES_HOME, its own gateway lock dir and XDG state, env -i.
# Usage: HERMES_SOURCE=<hermes-agent checkout with .venv> GLM_API_KEY=<key> tests/live/live_proof.sh <scratch-dir> <out-dir>
# The key goes into the gateway's environment only; it is never written to disk. Device tokens are random per run.
# Phase A (streaming on): v1 text, v1 keyword photo, v1 inline photo, agent photo tool, cron push fanned out to two
# devices, v0 text, offline outbox. Phase B (streaming off): v0 text, v1 text, v0 keyword photo, v0 cron push.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PIN="${HERMES_SOURCE:?set HERMES_SOURCE to a hermes-agent checkout with a .venv}"
S="${1:?scratch dir}"; OUT="${2:?out dir}"
GLM_KEY="${GLM_API_KEY:?set GLM_API_KEY}"
PORT="${PORT:-18765}"
PY="$PIN/.venv/bin/python"
SIM="$REPO/sim/glasses_sim.py"
URL="ws://127.0.0.1:$PORT/voice"

for d in "$S" "$OUT"; do
  [ ! -e "$d" ] || [ -z "$(ls -A "$d")" ] || { echo "$d must be empty or absent"; exit 2; }
done
mkdir -p "$S/home/plugins" "$S/home/scripts" "$S/locks" "$S/state" "$OUT"
H="$S/home"
TOK="gl-$(python3 -c 'import secrets;print(secrets.token_urlsafe(9))')"
TOK2="gl-$(python3 -c 'import secrets;print(secrets.token_urlsafe(9))')"
rsync -a --exclude .git --exclude tests --exclude .pytest_tmp --exclude __pycache__ "$REPO/" "$H/plugins/glasses-platform/"
printf '#!/bin/bash\necho "Cron reminder: stand up and stretch for two minutes."\n' > "$H/scripts/glasses_push.sh"
chmod +x "$H/scripts/glasses_push.sh"

ISO=(env -i HOME="$HOME" USER="$USER" LOGNAME="$USER" PATH=/usr/bin:/bin:/usr/sbin:/sbin
     HERMES_HOME="$H" HERMES_GATEWAY_LOCK_DIR="$S/locks" XDG_STATE_HOME="$S/state"
     HERMES_GATEWAY_MAX_STARTS=0 HERMES_STATE_DB_GUARD_BYPASS=1 HERMES_DISABLE_LAZY_INSTALLS=1)
GW=""
soon() { "$PY" -c 'import datetime as d; print((d.datetime.now(d.timezone.utc) + d.timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ"))'; }

write_config() { # $1 = true|false (gateway streaming)
  cat > "$H/config.yaml" <<YAML
model:
  default: glm-5.3
  provider: zai
auxiliary:
  vision:
    provider: zai
    model: glm-4.6v
    base_url: https://api.z.ai/api/coding/paas/v4
streaming:
  enabled: $1
updates:
  check: false
cron:
  preflight: false
platform_toolsets:
  glasses: [vision, glasses]  # no terminal: a real model must not get a shell on the test host
plugins:
  enabled: [glasses-platform]
platforms:
  glasses:
    enabled: true
    extra:
      host: 127.0.0.1
      port: $PORT
      devices: "owner:sim=$TOK,owner:hud=$TOK2"
YAML
  chmod 600 "$H/config.yaml"
}
start_gw() { # $1 = log name
  "${ISO[@]}" GLM_API_KEY="$GLM_KEY" GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4 \
    "$PY" -m gateway.run > "$S/$1.log" 2>&1 &
  GW=$!
  for _ in $(seq 1 90); do curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "gateway did not come up"; tail -40 "$S/$1.log"; return 1
}
stop_gw() { [ -n "$GW" ] || return 0; kill -TERM "$GW" 2>/dev/null
  for _ in $(seq 1 40); do ps -p "$GW" -o pid= >/dev/null 2>&1 || break; sleep 1; done; GW=""; }
trap stop_gw EXIT
run_chain() { # $1 = name; device 1 asks a long question, device 2 barges in 4 s later (in-band follow-up)
  GLASSES_SIM_TOKEN="$TOK" "$PY" "$SIM" --url "$URL" --device sim --timeout 90 \
    --say "Write a numbered list of 25 European capital cities, one per line." > "$OUT/$1-first.jsonl" 2> "$OUT/$1-first.stderr" &
  local first=$!; sleep 4
  local t0; t0=$(date +%s)
  GLASSES_SIM_TOKEN="$TOK2" "$PY" "$SIM" --url "$URL" --device hud --timeout 90 \
    --say "Quick question: what is 2 plus 2? Answer with the number only." > "$OUT/$1-second.jsonl" 2> "$OUT/$1-second.stderr"
  echo "$1-second rc=$? secs=$(( $(date +%s) - t0 ))" >> "$OUT/SUMMARY.txt"
  wait "$first"; echo "$1-first rc=$?" >> "$OUT/SUMMARY.txt"
}
run_sim() { local name="$1"; shift; local t0; t0=$(date +%s)
  GLASSES_SIM_TOKEN="$TOK" "$PY" "$SIM" --url "$URL" "$@" > "$OUT/$name.jsonl" 2> "$OUT/$name.stderr"
  echo "$name rc=$? secs=$(( $(date +%s) - t0 ))" | tee -a "$OUT/SUMMARY.txt"; }

"$PY" - "$S/sign.jpg" <<'PYIMG'
import sys
from PIL import Image, ImageDraw, ImageFont
im = Image.new("RGB", (1024, 640), (40, 90, 160)); d = ImageDraw.Draw(im)
d.rectangle([140, 160, 884, 480], fill=(250, 250, 245), outline=(20, 20, 20), width=8)
try: f = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 150)
except Exception: f = ImageFont.load_default()
d.text((512, 320), "GATE 42", fill=(200, 20, 20), font=f, anchor="mm")
im.save(sys.argv[1], "JPEG", quality=90)
PYIMG
cp "$S/sign.jpg" "$OUT/sign.jpg"

: > "$OUT/SUMMARY.txt"
echo "pin $(git -C "$PIN" describe --tags), plugin $(git -C "$REPO" rev-parse --short HEAD), port $PORT" >> "$OUT/SUMMARY.txt"

echo "== phase A: streaming on" >> "$OUT/SUMMARY.txt"
write_config true; start_gw gateway-A || exit 1
run_sim A1-v1-text   --device sim --say "In one short sentence, what can you help me with?"
run_sim A2-v1-photo  --device sim --timeout 240 --photo "$S/sign.jpg" --say "What am I looking at? Read the sign."
run_sim A2b-v1-inline --device sim --timeout 240 --inline-photo "$S/sign.jpg" --say "Describe the attached image in one sentence."
# Fresh session so the model cannot answer from A2's memory; the prompt avoids the keyword heuristic.
run_sim A3-v1-tool   --device sim --timeout 240 --new-session --photo "$S/sign.jpg" --say "Please call the glasses_capture_photo tool now, then tell me the number written on the sign."
"${ISO[@]}" "$PIN/.venv/bin/hermes" cron create "$(soon)" --no-agent --script glasses_push.sh --deliver glasses:owner --repeat 1 --name glasses-push-live > "$S/cron1.log" 2>&1
# Fan-out: a second device of the same user waits for the same push.
GLASSES_SIM_TOKEN="$TOK2" "$PY" "$SIM" --url "$URL" --device hud --wait-push 150 > "$OUT/A4-v1-push-hud.jsonl" 2> "$OUT/A4-v1-push-hud.stderr" &
HUD=$!
run_sim A4-v1-push   --device sim --wait-push 150
wait "$HUD"; echo "A4-v1-push-hud rc=$?" >> "$OUT/SUMMARY.txt"
run_sim A5-v0-text   --legacy --say "Say hello in five words."
"${ISO[@]}" "$PIN/.venv/bin/hermes" cron create "$(soon)" --no-agent --script glasses_push.sh --deliver glasses:owner --repeat 1 --name glasses-push-offline > "$S/cron2.log" 2>&1
sleep 90
run_sim A6-v1-outbox --device sim --wait-push 15
run_chain A7-chain
stop_gw

echo "== phase B: streaming off" >> "$OUT/SUMMARY.txt"
write_config false; start_gw gateway-B || exit 1
run_sim B1-v0-text   --legacy --say "Name one planet in one word."
run_sim B2-v1-text   --device sim --say "Name one colour in one word."
run_sim B3-v0-photo  --legacy --timeout 240 --photo "$S/sign.jpg" --say "What am I looking at? Read the sign."
"${ISO[@]}" "$PIN/.venv/bin/hermes" cron create "$(soon)" --no-agent --script glasses_push.sh --deliver glasses:owner --repeat 1 --name glasses-push-v0 > "$S/cron3.log" 2>&1
run_sim B4-v0-push   --legacy --wait-push 150
run_chain B5-chain
stop_gw

# Evidence hygiene: the device tokens and the model key must never appear in any log or transcript.
if grep -R -F -e "$TOK" -e "$TOK2" "$S"/gateway-*.log "$OUT" >/dev/null 2>&1; then echo "TOKEN LEAK FOUND" | tee -a "$OUT/SUMMARY.txt"; else echo "token-leak check: clean" >> "$OUT/SUMMARY.txt"; fi
for f in "$S"/gateway-A.log "$S"/gateway-B.log; do
  grep -i -E 'glasses|error|traceback' "$f" | grep -v -F -e "$GLM_KEY" | tail -60 > "$OUT/$(basename "$f" .log)-lines.txt"
done
grep -h -E '"type": "(response|push|capture_photo)"' "$OUT"/*.jsonl | cut -c1-400 > "$OUT/key-frames.txt"
"$PY" "$REPO/tests/live/live_checks.py" "$OUT" >> "$OUT/SUMMARY.txt" 2>&1
echo "done" >> "$OUT/SUMMARY.txt"
