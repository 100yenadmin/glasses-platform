# Hermes Glasses

![Hermes Glasses: your own Hermes agent, on your glasses](docs/media/banner.png)

Your own Hermes agent on smart glasses and voice devices: it sees what you see, answers by voice, and reminds you.

**[Watch the demo](https://x.com/ElectricSheepIO/status/2119822401361916227)**

[![Latest release](https://img.shields.io/github/v/release/100yenadmin/glasses-platform)](https://github.com/100yenadmin/glasses-platform/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Hermes >= 0.22.2](https://img.shields.io/badge/Hermes-%3E%3D%200.22.2-6f42c1)](https://hermes-agent.nousresearch.com)
[![CI](https://github.com/100yenadmin/glasses-platform/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/100yenadmin/glasses-platform/actions/workflows/ci.yml)

> **Status: v0.6.3 preview. Not yet tested on real glasses.** The Hermes side is proven against the bundled
> simulators and a real Hermes gateway with real models ([evidence](docs/evidence/M0-live-proof.md)); CI tests it on
> Hermes v0.22.2, v0.23.0 and `main`. Each device path below was run against a real Hermes gateway with a simulated
> device; no real glasses, watch, voice box or pendant has been tested yet.

## Install

```bash
hermes plugins install 100yenadmin/glasses-platform --enable
```

In a terminal, answer `y` to install the `aiohttp` and `qrcode` dependencies; without a terminal add `--yes-deps`.
(Without either, the plugin installs but stays disabled.) No API keys are needed. Needs Hermes Agent 0.22.2 or later
with in-process plugin loading, which is the default; the opt-in `plugins.isolation: host` mode cannot load platform
plugins.

## Try it (no glasses needed, about two minutes)

1. **Pair** a device. This makes a random token, saves it in the profile's `.env`, and prints a connect URL and QR code:
   ```bash
   hermes glasses pair            # device "glasses" for user "owner"
   hermes gateway restart         # the gateway loads devices when it starts
   ```
2. **Talk** to your agent from the terminal as a pair of glasses with a camera:
   ```bash
   hermes glasses try
   ```
   Then type `What am I looking at?` or `Remind me in one minute to stretch.` An empty line quits.
3. **What you will see:** `hermes  asks the glasses for a photo`, then `glasses  photo sent`, then your agent's
   short answer about the sample photo (an airport). The reminder prints as a push a minute later (the agent
   schedules it with its cron tool). Pass `--photo my.jpg` to show it your own picture.

Photo answers need a model that can see: a main model that accepts images, or an `auxiliary.vision` model in
`config.yaml`.

**Before you pair a real device:** a device token is full access to your agent. It bypasses the gateway's user
allowlist, chats as you, and can answer approval prompts and clarify questions. The listener is unencrypted and
listens on all interfaces (port 8765) by default: set `GLASSES_HOST=127.0.0.1` to keep it local, and use Tailscale
or a TLS reverse proxy away from your home network. Never post a connect URL or QR code; both carry the token.

## What it is

A [Hermes Agent](https://hermes-agent.nousresearch.com) **gateway platform plugin** that adds smart glasses as a
channel, the same way Hermes already talks to Telegram or Slack.

- A thin companion app on your phone (or the glasses' own app platform) connects over one WebSocket.
- Hermes keeps the conversation, memory, tools, cron jobs, heartbeat and approvals.

```
glasses ─▶ companion app ─WebSocket─▶ Hermes gateway (glasses platform) ─▶ your agent
```

## What you can do with it

- **Ask about what you see.** "What am I looking at?" takes a photo through the glasses before the question reaches
  the model. The agent can also decide to look by itself with its `glasses_capture_photo` tool.
- **Hear back later.** Reminders, cron jobs and heartbeat results arrive on your glasses as pushes. If no device is
  connected they wait, even across a gateway restart, and arrive when one reconnects.
- **Approve by voice.** When the agent wants to run a dangerous command, the glasses ask "Hermes wants to run: …
  Say yes to allow it once, or no to block it." A spoken yes or no answers that one request.
- **Talk instead of type.** Devices without their own speech recognition can send a recording; Hermes's own
  speech-to-text turns it into words. Devices that ask for it get every answer and reminder back as audio, spoken by
  Hermes's own text-to-speech.
- **Approve with a button.** Devices with buttons (a watch, a touchpad, a ring) get the approval as a card with
  Hermes's choices; the answer is bound to that one request.
- **Ask from your wrist.** Devices that cannot keep a connection open (an Apple Watch through a Siri Shortcut,
  scripts) use plain HTTP: `POST /turn` and `GET /pending`. See [docs/devices/apple-watch.md](docs/devices/apple-watch.md).
- **One conversation on every device.** Glasses and phone share one Hermes session per person: memory, schedules and
  tools are the same everywhere.
- **Set up in one command, try it without glasses.** `hermes glasses pair` prints a connect URL and QR code;
  `hermes glasses try` talks to your agent from the terminal as a pair of glasses with a camera.

## Devices

| Device | How it connects | Checked |
|---|---|---|
| Even Realities G2 glasses | [Even Hub app](clients/even-g2/README.md): tap to talk, answers on the lens, touchpad approvals, reminders | Even Hub simulator + real gateway |
| ESP32 voice boxes (xiaozhi firmware, 138+ boards) | [OTA pairing and the xiaozhi voice protocol](#esp32-voice-boxes-xiaozhi) | Simulated device + real gateway |
| Omi pendant | [Real-time transcript webhook](#omi-pendant): "Hey Hermes, …" | Webhook calls + real gateway |
| Pebble watches | [The open-source Bobby app](#pebble-bobby) with a two-line URL override | Bobby-protocol client + real gateway |
| Apple Watch, Siri Shortcuts, scripts | [HTTP route](docs/devices/apple-watch.md): `POST /turn`, `GET /pending` | Real gateway |
| Meta Ray-Ban (Gen 1/2), Ray-Ban Display, Oakley Meta | The community [hermes-glasses](https://github.com/prasanthsasikumar/hermes-glasses) iOS app speaks protocol v0 in its bridge mode | Bundled v0 simulator |
| Your own client | [JavaScript client](clients/js/hermes-glasses.js), [Python client](clients/python/README.md), [protocol](docs/PROTOCOL.md) and [conformance kit](conformance/README.md) | Conformance kit |

Planned: Android and Wear OS (M9), Mentra glasses (M11), and the Meta AI Connectors endpoint (gated on Meta early access).

## Pair more devices

Pair each device with one command. It creates a random token, saves it in the profile's `.env`, and prints the
connect URL and a QR code to open in the glasses app:

```bash
hermes glasses pair me rayban      # user "me", device "rayban"
hermes glasses pair me phone       # a second device: same user, same conversation
hermes glasses devices             # list paired devices (tokens are never shown)
hermes glasses revoke me phone     # remove one
hermes gateway restart             # the gateway loads devices when it starts
```

The URL points at this machine on your local network. Pass `--host` for another address (for example a Tailscale
name) and `--wss` behind a TLS proxy.

## ESP32 voice boxes (xiaozhi)

Run `hermes glasses pair me voicebox --xiaozhi` (add `--host` / `--port` if needed), then restart the gateway.
Paste the printed OTA address into the device's Wi-Fi setup page, under advanced settings → "OTA address".
Push to talk and hear the answer; server speech needs `ffmpeg` installed on the gateway.
Reminders play the next time the device opens its connection: xiaozhi devices only connect while talking.
They stay in the outbox until the bridge sends the normal playback `tts stop`; aborted or disconnected playback
is queued again on disconnect. This records socket delivery, not proof that the device played the sound.
Recordings stop at 15 seconds or 240,000 encoded Opus bytes (15 s × 16 kB/s). Opus packets over 1,500 bytes are
ignored; WebSocket messages are limited to 64 KiB. Ogg container work at 32 KiB or above runs in a worker thread.

<details><summary>Setting tokens by hand</summary>

Each entry names a user and a device (`user:device=...`); all devices of one user use one Hermes session.
Add a line like this to the profile's environment file:

```bash
GLASSES_DEVICES=me:rayban=<long-random-token>,me:g2=<another-token>
# or, for a single device:  GLASSES_TOKEN=<long-random-token>   (user "owner", device "default")
```

Then enable the platform in `config.yaml` and restart the gateway:

```yaml
platforms:
  glasses:
    enabled: true
streaming:
  enabled: true      # optional: stream replies to v1 clients as they are written
```

</details>

Photo turns need a model that can see: either a main model that accepts images, or an `auxiliary.vision` model in
`config.yaml`.

The plugin refuses to start without at least one token. It listens on all interfaces, port 8765, path `/voice`, by default.
Other settings are in [Settings and tools](#settings-and-tools).

To use it away from home Wi-Fi, put it behind Tailscale or a TLS reverse proxy. The plugin does not terminate TLS.

## Omi pendant

1. Pair with `hermes glasses pair me omi`, keep the token from the pairing URL, then run `hermes gateway restart`.
2. In Omi → Settings → Developer Mode → Developer Settings → Real-Time Transcript Webhook, paste `https://<public-host>/omi/<device-token>/transcript`.
3. Start a live Omi conversation and say “Hey Hermes, what time is it?”; the answer arrives as an Omi notification.
4. Say “Hey Hermes, yes” or “Hey Hermes, no” for approvals; reminders sent to `glasses:me` arrive while the conversation is live.

Omi's servers call this URL, so it must be reachable from the internet through an HTTPS reverse proxy or
Tailscale Funnel. Keep the URL private and keep tokens out of proxy access logs. The plugin makes no outbound
Omi calls. Replies taking longer than `omi_wait` seconds (default 20, `GLASSES_OMI_WAIT`) wait for the next
transcript call, as do pushes sent while the pendant is offline. Messages start with `Hermes: ` so even short
answers display. Within 1000 characters, items appear oldest first; an item that exceeds the remaining room
keeps its head and ends in `…`, counting as shown. Items with no room stay queued for the next call.
When a call has no `is_user: true` segment, every speaker counts for questions, but only speech marked
`is_user: true` (your own voice) can answer an approval.
Overlapping webhook calls can show the same push twice. Session buffers expire after ten minutes idle and
retain at most 256 segment keys and 32 sessions per device identity. This path is tested with fakes;
no Omi account has been tested.

## Pebble (Bobby)

Build the open-source [Bobby assistant](https://github.com/pebble-dev/bobby-assistant) (Apache-2.0) with the Pebble
SDK installed. Pair a device with `hermes glasses pair <user> pebble`, then create
`app/src/pkjs/urls_override.js` in your Bobby checkout with exactly these two exports (use your plugin device token):

```javascript
exports.QUERY_URL = 'wss://<host>/bobby/<token>/query';
exports.QUOTA_URL = 'https://<host>/bobby/<token>/quota';
```

From the Bobby checkout, build and install with the phone's developer connection enabled:

```sh
cd app
npm install
pebble build
pebble install --phone <phone-ip>
```

The phone must reach the gateway through Tailscale or an HTTPS proxy; Pebble's JavaScript needs `wss`/`https` for
non-local hosts. The proxy must forward WebSocket upgrades. Keep the device URL private. Ask a question on the
watch: Hermes streams its answer, followed by queued reminders and pushes. Approval or clarify questions appear
as text; ask “yes” or “no” next. After `bobby_wait` seconds (`GLASSES_BOBBY_WAIT`, default 45), Bobby finishes with
a still-working message; the late answer is queued for your next question.

Bobby's own widgets and actions (including watch timers and reminders) are unsupported. Hermes reminders appear
as plain text with the next answer. This endpoint has aiohttp client tests; a real-watch session is still needed.

## More ways to try it

**A demo gateway:** `demo/run_demo_gateway.sh <empty-dir>` starts an isolated Hermes gateway with this plugin, a
generated device token and only the vision, glasses, cronjob and memory toolsets (it uses the z.ai GLM coding plan;
see the script's header).

For scripted runs, `sim/glasses_sim.py --url "<connect URL>" --say "..." --pretty` talks to a gateway without a
prompt.

**With the hermes-glasses iOS app** (untested so far): in the app, set Settings → Assistant → Backend to Bridge,
and point it at `ws://<your-mac>.local:8765/voice?token=<token>`.

**Scheduled messages:** send them to your glasses with `--deliver glasses:<user>`, for example
`hermes cron create "every day at 9am" "Brief me on today" --deliver glasses:me`. They arrive as pushes, or wait in a
short offline queue until a device reconnects.

## Settings and tools

Set these in the profile's `.env` (or under `platforms.glasses.extra` in `config.yaml`), then restart the gateway.
`hermes glasses pair` sets the device tokens for you.

| Setting | Default | What it does |
|---|---|---|
| `GLASSES_DEVICES` | none | Device tokens, `user:device=token,...`; all of one user's devices share one Hermes session |
| `GLASSES_TOKEN` | none | Shorthand for one device, `owner:default` |
| `GLASSES_HOST` / `GLASSES_PORT` | `0.0.0.0` / `8765` | Listener address; `127.0.0.1` keeps it on this machine |
| `GLASSES_HOME_CHANNEL` | first user | Who gets `--deliver glasses` cron jobs and notifications |
| `GLASSES_AUTO_CAPTURE` | `keywords` | Take a photo when you ask something like "what am I looking at?"; `off` leaves it to the agent |
| `GLASSES_OUTBOX_PERSIST` | `on` | Keep pushes for offline devices in `$HERMES_HOME/glasses/outbox.json` across restarts |
| `GLASSES_TURN_TIMEOUT` | `50` | Seconds an HTTP `POST /turn` waits before answering `pending` |
| `GLASSES_OMI_WAIT` / `GLASSES_BOBBY_WAIT` | `20` / `45` | Seconds Omi and Pebble requests wait for the answer |
| Speech and vision | Hermes's own | Voice uses Hermes's `stt` and `tts` settings; photos need a model that can see or an `auxiliary.vision` model |

The full list, including photo timeout, frame size and outbox limits, is in [docs/PLAN.md](docs/PLAN.md#6-configuration).

The agent gets three tools (toolset `glasses`) while it talks to a glasses user:

| Tool | What it does |
|---|---|
| `glasses_capture_photo` | Takes a photo with the glasses camera and looks at it |
| `glasses_show_text` | Shows text on the glasses display (up to 500 characters, optional seconds) |
| `glasses_buzz` | Buzzes the glasses: `tap`, `double` or `long` |

## What it does on your machine

- **Opens a network listener.** A WebSocket server on all interfaces, port 8765, path `/voice`, plus the HTTP
  routes `/turn`, `/pending`, `/approval`, `/omi/{token}/transcript`, `/xiaozhi/ota/{token}`, `/bobby/{token}/quota`
  and the `/xiaozhi` and `/bobby/{token}/query` WebSockets on the same port and with the same tokens (set `GLASSES_HOST`
  to `127.0.0.1` to keep it local). It refuses to start until at least one device token is configured.
- **A device token is full access to your agent.** Anyone holding one can chat with your agent as you, and can answer
  the approval prompts and clarify questions the agent sends to your devices. Keep tokens long, random and private.
- **Photos.** Photos from the glasses are saved in Hermes's own image cache, like photos sent from any other channel.
- **New session.** The device's new-session button answers Hermes's `/new` confirmation for that user's own session.
- **Approvals by voice or button.** When the agent wants to run a dangerous command, the glasses ask a short question
  ("Hermes wants to run: … Say yes to allow it once, or no to block it."). The question carries the whole command,
  up to 300 characters. Only a whole "yes" (allow once, never for the session or always) or "no" from one of that
  user's own devices answers it; anything else goes to the agent as usual. Devices with buttons get the same approval
  as a card and can pick any choice Hermes offers for that request. A longer command is shown clipped, so a device
  can only block it: a spoken yes or an approve button does nothing, and you allow it with a typed `/approve` after
  reading the full command. The same goes for a spoken yes when a device heard the prompt through Hermes's
  text-to-speech and the speech dropped part of the command (it skips paths and web addresses). The G2 app offers
  only Deny when its lens cannot show the whole command. Typed `/approve` and `/deny` still work. The plugin never
  answers an approval by itself.
- **Speech.** Recordings from devices are transcribed, and spoken answers made, with Hermes's own speech settings
  (`stt` and `tts` in Hermes's config). With a cloud speech provider, the recording or the reply text goes to that
  provider, as it does for voice messages on any other channel. Recordings are cached like other Hermes audio.
- **`hermes glasses pair`** writes the new device's token to `GLASSES_DEVICES` in the profile's `.env` (keeping the
  devices already set there or in `config.yaml`), and prints the connect URL. `revoke` removes one.
- **No third-party calls.** The plugin itself makes no outbound network requests and sends no telemetry. The only
  program it runs is a local `ffmpeg` (no shell), and only to convert audio for devices that ask for Ogg or WAV or
  when a spoken answer comes in several parts.
- **Device credentials** are read through Hermes's per-profile settings reader (the profile's environment file or `config.yaml`).
- **Offline queue.** Pushes for a disconnected device wait (50 items, 24 hours) in memory and in
  `~/.hermes/glasses/outbox.json` (readable only by you), so they survive a gateway restart, even a crash. A message leaves
  the file once it has been sent to a device. `GLASSES_OUTBOX_PERSIST=off` keeps the queue in memory only.

## Why a plugin rather than a bridge script

- **Existing bridges** start a new Hermes CLI process for every utterance. They cannot reach the glasses after the
  turn ends, and they lose streaming and approvals.
- **A gateway platform** keeps one live session per person across all their devices. Cron and heartbeat results
  arrive as pushes, and replies stream to a heads-up display.

## Build a companion client

The dependency-free [JavaScript client](clients/js/README.md) speaks protocol v1 over
WebSocket or HTTP, with injectable transports for phone runtimes. The
[conformance kit](conformance/README.md) checks a server or runs a scripted local
server for client authors. Planned server features still depend on the installed
server's advertised capabilities.

## Troubleshooting

- **The plugin is installed but disabled.** Its dependencies were skipped.
  Reinstall in a terminal and answer `y`: `hermes plugins install 100yenadmin/glasses-platform --force --enable`
  (or add `--yes-deps` without a terminal).
- **`pair` printed no QR code.** The `qrcode` package is missing from the plugin's environment. Reinstall as above,
  then `hermes glasses pair <user> <device> --replace` for a QR code with a new token. The printed URL works meanwhile.
- **The device cannot connect.** Restart the gateway after pairing (`hermes gateway restart`). Check that the phone
  or device can reach this machine on port 8765 (same Wi-Fi, firewall, or `--host` with a Tailscale name), and that
  a TLS proxy forwards WebSocket upgrades. A connection closed with code 4401 means a wrong or revoked token.
- **The glasses platform does not start.** It refuses to start without at least one device token; run
  `hermes glasses pair`, then restart the gateway.
- **No spoken answers or recordings are not understood.** Speech uses Hermes's own `stt` and `tts` settings; check
  them with any other voice channel. ESP32 voice boxes and Ogg or WAV devices also need `ffmpeg` on the gateway.
- **Photo questions get no description.** Configure a model that can see (see [Try it](#try-it-no-glasses-needed-about-two-minutes)).

## Report a bug

[Open an issue](https://github.com/100yenadmin/glasses-platform/issues/new) with your Hermes version
(`hermes --version`), the plugin version, the device and what happened. **Never paste a connect URL, QR code,
`.env` line or log line that contains a device token**; replace the token with `<token>`. Revoke and re-pair any
device whose token was shared (`hermes glasses revoke <user> <device>`).

For a security problem, [report it privately](https://github.com/100yenadmin/glasses-platform/security/advisories/new)
instead of opening a public issue.

## Links

[Releases](https://github.com/100yenadmin/glasses-platform/releases) ·
[CHANGELOG](CHANGELOG.md) · [Roadmap](ROADMAP.md) · [Design plan](docs/PLAN.md) · [Device protocol](docs/PROTOCOL.md) ·
[Contributing](CONTRIBUTING.md) · [License (MIT)](LICENSE)
