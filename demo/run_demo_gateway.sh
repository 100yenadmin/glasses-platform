#!/bin/bash
# Start an isolated Hermes gateway with the glasses platform, for demos and dry runs.
#
# Usage:
#   HERMES_SOURCE=<hermes-agent checkout with .venv> GLM_API_KEY=<key> demo/run_demo_gateway.sh <empty-demo-dir>
#
# Optional env: GLASSES_DEMO_HOST (default 0.0.0.0, so a phone on the same Wi-Fi can connect),
# GLASSES_DEMO_PORT (default 8765).
#
# Everything lives in <demo-dir>: a throwaway Hermes home with its own gateway lock dir, so nothing is shared with
# any other Hermes on the machine. The plugin is copied from this checkout's HEAD. The model is the z.ai GLM coding
# plan: glm-5.3 as the main model and glm-4.6v for vision. The agent gets the vision, glasses, cronjob and memory
# toolsets and no terminal, so the model has no shell on this machine.
#
# A device token is generated into <demo-dir>/device-token (mode 600); the script never prints it.
# The gateway runs in the foreground; stop it with Ctrl-C.
set -euo pipefail

DEMO="${1:?usage: demo/run_demo_gateway.sh <empty-demo-dir>}"
: "${HERMES_SOURCE:?set HERMES_SOURCE to a hermes-agent checkout with a .venv}"
: "${GLM_API_KEY:?set GLM_API_KEY}"
HOST="${GLASSES_DEMO_HOST:-0.0.0.0}"
PORT="${GLASSES_DEMO_PORT:-8765}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERMES_SOURCE/.venv/bin/python"

if [ -e "$DEMO" ] && [ -n "$(ls -A "$DEMO" 2>/dev/null)" ]; then
  echo "refusing: $DEMO is not empty (use a new directory for each demo home)" >&2
  exit 1
fi
mkdir -p "$DEMO/home/plugins/glasses-platform" "$DEMO/locks" "$DEMO/state"
H="$DEMO/home"
git -C "$REPO" archive HEAD | tar -x -C "$H/plugins/glasses-platform"
rm -rf "$H/plugins/glasses-platform/tests" "$H/plugins/glasses-platform/demo"

umask 077
"$PY" -c 'import secrets; print("gl-" + secrets.token_urlsafe(24))' > "$DEMO/device-token"
TOKEN="$(cat "$DEMO/device-token")"
# Stamp the config with this Hermes's schema version so it is not treated as an old config to migrate.
VERSION="$(cd "$HERMES_SOURCE" && HERMES_HOME="$H" "$PY" -c 'from hermes_cli.config import DEFAULT_CONFIG as D; print(D["_config_version"])' 2>/dev/null || true)"
[ -n "$VERSION" ] && echo "_config_version: $VERSION" > "$H/config.yaml"
cat >> "$H/config.yaml" <<YAML
model:
  default: glm-5.3
  provider: zai
auxiliary:
  vision:
    provider: zai
    model: glm-4.6v
    base_url: https://api.z.ai/api/coding/paas/v4
streaming:
  enabled: true
updates:
  check: false
onboarding:
  profile_build: "off"  # no first-contact profile offer appended to the first answer
sessions:
  auto_prune: false  # answers Hermes's one-time disk-space question, so it is never sent to the device
cron:
  preflight: false
platform_toolsets:
  glasses: [vision, glasses, cronjob, memory]
plugins:
  enabled: [glasses-platform]
platforms:
  glasses:
    enabled: true
    extra:
      host: $HOST
      port: $PORT
      devices: "owner:phone=$TOKEN"
YAML

echo "glasses demo gateway: ws://<this-machine>:$PORT/voice?token=<the token in $DEMO/device-token>"
exec env -i HOME="$HOME" USER="${USER:-demo}" LOGNAME="${USER:-demo}" PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  HERMES_HOME="$H" HERMES_GATEWAY_LOCK_DIR="$DEMO/locks" XDG_STATE_HOME="$DEMO/state" \
  HERMES_GATEWAY_MAX_STARTS=0 HERMES_STATE_DB_GUARD_BYPASS=1 HERMES_DISABLE_LAZY_INSTALLS=1 \
  GLM_API_KEY="$GLM_API_KEY" GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4 \
  "$PY" -m gateway.run
