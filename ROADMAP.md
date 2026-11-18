# Roadmap

Each milestone has a GitHub milestone and a tracking issue. Order is by dependency. A milestone closes only when
its exit criteria are proven, not when its code merges. A box is ticked only when its proof is linked (tests or a
live proof), not when its code merges.

**Where it stands (2026-11-18):**
- **Committed next:** the open software work in M1, streaming voice out ([#14](https://github.com/100yenadmin/glasses-platform/issues/14)) and the folded barge-in
  media bug ([#72](https://github.com/100yenadmin/glasses-platform/issues/72)).
- **Waiting on real devices (an owner decision):** M2, M4's exit criterion, and M5–M11. The server side of M5, M6,
  M7, M8 and M10 has shipped; each still needs a recorded real-device session. M9 and M11 have no client yet.

## M0 — Design + scaffold (done)

Plan, protocol, and a working Hermes-side channel driven by a simulator.

- [x] Design and protocol docs ([DESIGN.md](docs/DESIGN.md), [PROTOCOL.md](docs/PROTOCOL.md))
- [x] Plugin skeleton: `plugin.yaml` (`kind: platform`), `register(ctx)`, `GlassesAdapter`
- [x] Token auth (fail closed, `4401`), device registry, one session per user
- [x] v0 compatibility with the hermes-glasses iOS bridge protocol
- [x] v1 handshake, `response_delta` streaming, `status`, `push`
- [x] Photo turns: keyword pre-capture plus the `glasses_capture_photo` tool
- [x] Offline outbox (in-memory) and cron delivery to `glasses:<user>`
- [x] Python simulator (`sim/glasses_sim.py`)
- [x] Unit tests, plus an opt-in integration test (real gateway, fake model)

**Exit criteria:**
- Unit and integration tests are green.
- `hermes plugins validate` passes.
- Live proof against a real gateway and a real model: a text turn, a photo turn and a cron push, in both v1 and v0 mode.

**Evidence:** [docs/evidence/M0-live-proof.md](docs/evidence/M0-live-proof.md). Proven against the simulator and a real
Hermes gateway with real models only; no real glasses or phone app yet (that is M2).

## M1 — Complete channel

- [x] Voice in (0.4.0): devices send audio (`audio` frame, or `query.audio`); the adapter wraps it as WAV and hands it to
  Hermes as a voice message, so Hermes's own speech-to-text transcribes it. Needed by devices with no speech
  recognition of their own (ESP32 voice boxes, watches, pendants).
- [ ] Voice out (server-side TTS), partly shipped. Since 0.4.0, devices that declare `audio_out` get each answer and
  push as audio from Hermes's own text-to-speech, as one whole reply. Still open ([#14](https://github.com/100yenadmin/glasses-platform/issues/14)): Hermes's
  streaming-TTS hooks sending PCM16 frames (24 kHz, or the rate the provider reports), and auto-TTS for voice turns
  that follows Hermes's `/voice` setting.
- [x] Approvals by voice (0.2.0): a dangerous-command approval is asked as a yes/no question, and a spoken "yes"
  (allow once) or "no" answers it.
- [x] Native `approval_request` / `approval_response` frames (0.4.0) for devices with buttons (watch tap, touchpad, ring),
  bound to Hermes's approval request id (0.23+, with the current fallback on 0.22).
- [x] `interrupt` → `/stop` (0.2.1)
- [x] Durable outbox: queued pushes survive a gateway restart (0.3.0); `ack` from v1 devices (0.4.0)
- [x] `hermes glasses pair <user> <device>`: mint a device token and print a QR / connect URL (0.2.0), plus
  `devices`, `revoke` and `try` (talk to your agent from the terminal as a pair of glasses with a camera)
- [x] CI matrix (Hermes v0.22.2, v0.23.0 and main), product-page README (0.6.1), community kit (bug form, SECURITY.md,
  AGENTS.md)

**Exit criteria:** every M1 frame is covered by tests, and the live proof covers voice in, voice out and approvals.

## M2 — First real glasses: Meta Ray-Ban via hermes-glasses

- [ ] Run the hermes-glasses iOS app unchanged against the plugin (v0) on real Ray-Ban Meta glasses
- [ ] Propose v1 support to hermes-glasses upstream (deltas, push, inline photo); fork only if declined
- [ ] Real-device proof: voice turn, photo turn, a push arriving while idle, a Ray-Ban Display lens render

**Exit criteria:** recorded real-device sessions with pass rates. Simulator runs do not count.

## M3 — Catalog 1.0 (done)

- [x] Hermes plugin catalog entry: pinned sha, house-style banner, real screenshots, a disclosure sentence (0.6.2, then
  re-pinned to 0.6.6)
- [x] Release notes ([CHANGELOG](CHANGELOG.md) and GitHub releases), security model ([SECURITY.md](SECURITY.md) and
  the README's "What it does on your machine"), compatibility (Hermes 0.22.2 or later; CI on v0.22.2, v0.23.0 and main)

**Exit criteria:** the catalog PR is merged and `hermes plugins install glasses-platform` works from the catalog.
Met on 2026-11-13: a fresh Hermes 0.23.1 home with no terminal installed 0.6.6 by name from the live catalog,
enabled it, paired a device and answered a real photo question.

## M4 — Any-device core

The plugin becomes the hub for any device that is not a desktop: glasses, watches, speakers, pendants, cars. One
session per person across all their devices stays the core. Devices differ only in what they can do and how they
connect. See [docs/DEVICES.md](docs/DEVICES.md) for the device survey behind this plan.

- [x] Capability declaration (0.4.0): `hello.device.capabilities` becomes a documented list (`camera`, `display` with
  size, `speaker`, `mic`, `haptics`, `buttons`, `location`), and the server picks devices by capability. Modelled
  on OpenClaw's node handshake.
- [x] Device actions for the agent (0.4.0): next to `glasses_capture_photo`, the agent can show text on a display
  (`glasses_show_text`) and buzz (`glasses_buzz`), each only on devices that declared it.
- [ ] Read location from devices that declare `location` (not built yet).
- [x] HTTP route (0.4.0) next to the WebSocket, same tokens: `POST /turn` (text or audio in, one short reply out) and a
  long-poll `GET /pending` for pushes and approvals. For devices that cannot hold a WebSocket: Apple Watch
  (watchOS allows HTTP(S) requests but WebSockets only during audio or calls), Garmin, Siri Shortcuts, polling clients.
- [x] Conformance kit (0.5.0): the simulator's scenarios as a test any client can run against a server, so a
  third-party app can show it works with Hermes.
- [x] Small client libraries: JavaScript (0.5.0, used by the G2 app); Python (0.6.0).

**Exit criteria:** the conformance kit passes against the plugin over both the WebSocket and the HTTP route, and
two different device classes complete a real-device session.

## M5–M11 — Device bridges, in order of reach

Each bridge has its own GitHub milestone. **Exit criteria per bridge:** a recorded real-device session with pass
rates. Simulator runs prove the server side only.

### M5 — Apple Watch and Siri Shortcuts ([#22](https://github.com/100yenadmin/glasses-platform/issues/22))

The largest smartwatch base: a Shortcut on the HTTP route first, then a watchOS app.

- [x] Siri Shortcut recipe (Dictate → `POST /turn` → Speak) with setup steps:
  [docs/devices/apple-watch.md](docs/devices/apple-watch.md) (0.4.0). Proof: the route it calls is tested in
  [tests/test_http_route.py](tests/test_http_route.py).
- [ ] Test the Shortcut on a real Apple Watch
- [ ] watchOS app: `/turn` and `/pending`, with push notifications for server messages

### M6 — ESP32 voice boxes (xiaozhi) ([#23](https://github.com/100yenadmin/glasses-platform/issues/23))

A xiaozhi-compatible endpoint covers the 138+ boards of the open-source xiaozhi firmware.

- [x] xiaozhi-compatible WebSocket endpoint: hello handshake, bearer and Device-Id headers, Opus audio, listen and
  abort, tts and stt events (0.5.0). Proof: [tests/test_xiaozhi.py](tests/test_xiaozhi.py).
- [x] Pairing for xiaozhi devices: `hermes glasses pair <user> <device> --xiaozhi` prints the OTA URL (0.5.0). Proof:
  the OTA tests in [tests/test_xiaozhi.py](tests/test_xiaozhi.py).
- [x] xiaozhi device simulator: [sim/xiaozhi_sim.py](sim/xiaozhi_sim.py) (0.5.0). Proof: the README's device table
  records its check, a simulated device against a real gateway ([ESP32 voice boxes](README.md#esp32-voice-boxes-xiaozhi)).
- [ ] Optional: bridge the device's MCP tools into Hermes tools
- [ ] Real-board session

### M7 — Even Realities G2 ([#24](https://github.com/100yenadmin/glasses-platform/issues/24))

- [x] Even Hub web app on our protocol: glasses mic, reply text on the display, touchpad and ring for approvals,
  pushes. Client in [clients/even-g2](clients/even-g2/README.md) (0.5.0). Proof:
  [clients/even-g2/test/g2.test.mjs](clients/even-g2/test/g2.test.mjs), and a check in the Even Hub simulator against
  a real gateway.
- [ ] A build testers can install. Beta testers need no store review; today you build and pack it yourself.
- [ ] Real-glasses session

### M8 — Omi pendant ([#25](https://github.com/100yenadmin/glasses-platform/issues/25))

- [x] Live transcripts by webhook into the user's session: `POST /omi/<token>/transcript` (0.5.0). Proof:
  [tests/test_omi.py](tests/test_omi.py).
- [x] Replies to the pendant: answers, reminders and approval prompts return as Omi notifications (0.5.0). Proof:
  [tests/test_omi.py](tests/test_omi.py).
- [ ] Real-pendant session

### M9 — Android: Wear OS and Android Auto ([#26](https://github.com/100yenadmin/glasses-platform/issues/26))

- [ ] Android reference client (phone and Wear OS) on the v1 protocol
- [ ] Messaging notifications with reply actions, which Android Auto shows
- [ ] Real-device sessions

### M10 — Pebble ([#27](https://github.com/100yenadmin/glasses-platform/issues/27))

- [x] Bobby talks to Hermes without a fork: the plugin serves Bobby-compatible query and quota endpoints with
  streamed answers and queued pushes, and Bobby is built with a two-line `urls_override.js` (0.5.0). Proof:
  [tests/test_pebble.py](tests/test_pebble.py), and a live test with a Bobby-protocol client against a real gateway.
- [ ] Real-watch session

### M11 — Mentra glasses ([#28](https://github.com/100yenadmin/glasses-platform/issues/28))

Mentra Live and MentraOS (also G1, G2 and Vuzix through MentraOS).

- [ ] Mentra Live Bluetooth SDK client, or a MentraOS app that relays to our protocol
- [ ] Real-glasses session

Meta Ray-Ban stays M2 (via hermes-glasses); public reach waits on Meta opening its device toolkit. The Meta AI
Connectors MCP endpoint (`ask_hermes`, `hermes_task_status`) is on the watch list, gated on Meta early access.

## Covered elsewhere, so not built here

- Home Assistant speakers and satellites (Voice PE, ESPHome boxes, hacked Echos, Nest board swaps): use
  hermes-voice or Hermes's own OpenAI-compatible API server; HA does the speech.
- Even G2's built-in "Agent Configuration" slot: point it at Hermes's OpenAI-compatible API server.

## Watch list

Meta AI Connectors, Android XR glasses (Jetpack Projected, alpha), Brilliant Labs Halo, Snap Specs, Rabbit R1 (rabbitOS agent),
Garmin (Connect IQ 9.3 microphone access), CarPlay (needs Apple's entitlement), Ray-Ban Meta Gen 3 once the toolkit
supports it.

## Not planned

Direct Wyoming satellites (the Wyoming satellite project is archived), WebRTC realtime stacks, Echo- or
Nest-specific firmware, Humane Ai Pin, Brilliant Frame/Noa, Plaud, Flipper.
