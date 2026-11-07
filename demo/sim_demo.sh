#!/bin/bash
# The demo storyboard on the bundled simulator, as a readable transcript, against a gateway started by
# demo/run_demo_gateway.sh. No phone or glasses needed; everything shown is the real exchange with Hermes.
#
# Usage:
#   HERMES_SOURCE=<hermes-agent checkout with .venv> demo/sim_demo.sh <demo-dir> [ws://127.0.0.1:8765/voice]
#
# Beats: a photo question (the simulator answers the camera request with the sign image in docs/evidence/),
# a one-minute reminder (DEMO_REMINDER overrides its wording), a memory, a new session that recalls it, and the reminder arriving on its own.
set -uo pipefail

DEMO="${1:?usage: demo/sim_demo.sh <demo-dir> [ws-url]}"
URL="${2:-ws://127.0.0.1:${GLASSES_DEMO_PORT:-8765}/voice}"
: "${HERMES_SOURCE:?set HERMES_SOURCE to the hermes-agent checkout the gateway runs from}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERMES_SOURCE/.venv/bin/python"
SIGN="$REPO/docs/evidence/m0-live-proof/sign.jpg"
GLASSES_SIM_TOKEN="$(cat "$DEMO/device-token")"
export GLASSES_SIM_TOKEN

sim() { "$PY" "$REPO/sim/glasses_sim.py" --url "$URL" --device phone --pretty "$@"; }

echo "glasses simulator  <->  Hermes gateway (glasses platform)"
echo
sim --new-session --timeout 240 --photo "$SIGN" \
  --say "Hermes, what am I looking at?" \
  --say "${DEMO_REMINDER:-Remind me in one minute to call Sam about gate 42.}" \
  --say "Save to memory: my gate is 42." || exit $?
echo
sim --new-session --timeout 120 --wait-push 180 --say "What's my gate?" || true  # ends when the reminder lands
