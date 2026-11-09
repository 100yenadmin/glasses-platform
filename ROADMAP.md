# Roadmap

Each milestone has a GitHub milestone and a tracking issue. Order is by dependency. A milestone closes only when
its exit criteria are proven, not when its code merges.

## M0 — Design + scaffold (done)

Plan, protocol, and a working Hermes-side channel driven by a simulator.

- [x] Design plan and protocol docs ([PLAN.md](docs/PLAN.md), [PROTOCOL.md](docs/PROTOCOL.md))
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

- [ ] Voice in: devices send audio (`audio` frame, or `query.audio`); the adapter wraps it as WAV and hands it to
  Hermes as a voice message, so Hermes's own speech-to-text transcribes it. Needed by devices with no speech
  recognition of their own (ESP32 voice boxes, watches, pendants).
- [ ] Voice out (server-side TTS): implement Hermes's streaming-TTS hooks to send PCM16 frames (24 kHz, or the rate
  the provider reports), plus a whole-reply fallback. Auto-TTS for voice turns follows Hermes's `/voice` setting.
- [x] Approvals by voice (0.2.0): a dangerous-command approval is asked as a yes/no question, and a spoken "yes"
  (allow once) or "no" answers it.
- [ ] Native `approval_request` / `approval_response` frames for devices with buttons (watch tap, touchpad, ring),
  bound to Hermes's approval request id (0.23+, with the current fallback on 0.22).
- [x] `interrupt` → `/stop` (0.2.1)
- [x] Durable outbox: queued pushes survive a gateway restart (0.3.0); `ack` from v1 devices still planned
- [x] `hermes glasses pair <user> <device>`: mint a device token and print a QR / connect URL (0.2.0), plus
  `devices`, `revoke` and `try` (talk to your agent from the terminal as a pair of glasses with a camera)
- [ ] CI matrix (pinned Hermes release + main), community kit, product-page README

**Exit criteria:** every M1 frame is covered by tests, and the live proof covers voice in, voice out and approvals.

## M2 — First real glasses: Meta Ray-Ban via hermes-glasses

- [ ] Run the hermes-glasses iOS app unchanged against the plugin (v0) on real Ray-Ban Meta glasses
- [ ] Propose v1 support to hermes-glasses upstream (deltas, push, inline photo); fork only if declined
- [ ] Real-device proof: voice turn, photo turn, a push arriving while idle, a Ray-Ban Display lens render

**Exit criteria:** recorded real-device sessions with pass rates. Simulator runs do not count.

## M3 — Catalog 1.0

- [ ] Hermes plugin catalog entry: pinned sha, house-style banner, real screenshots, a disclosure sentence
- [ ] Release notes, security model and compatibility table

**Exit criteria:** the catalog PR is merged and `hermes plugins install glasses-platform` works from the catalog.

## M4 — Any-device core

The plugin becomes the hub for any device that is not a desktop: glasses, watches, speakers, pendants, cars. One
session per person across all their devices stays the core. Devices differ only in what they can do and how they
connect. See [docs/DEVICES.md](docs/DEVICES.md) for the device survey behind this plan.

- [ ] Capability declaration: `hello.device.capabilities` becomes a documented list (`camera`, `display` with
  size, `speaker`, `mic`, `haptics`, `buttons`, `location`), and the server picks devices by capability. Modelled
  on OpenClaw's node handshake.
- [ ] Device actions for the agent: generalise `glasses_capture_photo` to show text on a display, buzz, or read
  location, each only on devices that declared it.
- [ ] HTTP route next to the WebSocket, same tokens: `POST /turn` (text or audio in, one short reply out) and a
  long-poll `GET /pending` for pushes and approvals. For devices that cannot hold a WebSocket: Apple Watch
  (watchOS only allows plain HTTP outside audio or calls), Garmin, Siri Shortcuts, polling clients.
- [ ] Conformance kit: the simulator's scenarios as a test any client can run against a server, so a third-party
  app can show it works with Hermes.
- [ ] Small client libraries: JavaScript (browser, Even Hub, Pebble's phone-side JS) and Python first.

**Exit criteria:** the conformance kit passes against the plugin over both the WebSocket and the HTTP route, and
two different device classes complete a real-device session.

## M5 — Device bridges (in priority order)

- [ ] **Even Realities G2:** an Even Hub web app on our protocol (glasses mic, display, touchpad and ring for
  approvals, pushes). Beta testers need no store review.
- [ ] **ESP32 voice boxes:** a xiaozhi-compatible endpoint (its WebSocket hello, Opus audio, abort), which covers
  the 138+ boards that the open-source xiaozhi firmware supports. Needs M1 voice in and out.
- [ ] **Apple Watch:** a Siri Shortcut on the HTTP route first (test it on a real watch), then a watchOS app with
  push notifications.
- [ ] **Pebble:** point the open-source Bobby assistant's phone-side WebSocket at our protocol.
- [ ] **Omi pendant:** an Omi app that receives live transcripts by webhook and feeds the user's session.
- [ ] **Meta Ray-Ban / Ray-Ban Display:** via the hermes-glasses app (M2); public reach waits on Meta opening its
  device toolkit to everyone.
- [ ] **Mentra Live and MentraOS glasses** (also G1, G2 and Vuzix through MentraOS).
- [ ] **Wear OS and Android Auto:** one Android reference client; Android Auto shows its messages for free.
- [ ] **Meta AI Connectors MCP endpoint** (`ask_hermes`, `hermes_task_status`), gated on Meta early access.

**Exit criteria per bridge:** a recorded real-device session with pass rates. Simulator runs do not count.

## Covered elsewhere, so not built here

- Home Assistant speakers and satellites (Voice PE, ESPHome boxes, hacked Echos, Nest board swaps): use
  hermes-voice or Hermes's own OpenAI-compatible API server; HA does the speech.
- Even G2's built-in "Agent Configuration" slot: point it at Hermes's OpenAI-compatible API server.

## Watch list

Android XR glasses (Jetpack Projected, alpha), Brilliant Labs Halo, Snap Specs, Rabbit R1 (rabbitOS agent),
Garmin (Connect IQ 9.3 microphone access), CarPlay (needs Apple's entitlement), Ray-Ban Meta Gen 3 once the toolkit
supports it.

## Not planned

Direct Wyoming satellites (the Wyoming satellite project is archived), WebRTC realtime stacks, Echo- or
Nest-specific firmware, Humane Ai Pin, Brilliant Frame/Noa, Plaud, Flipper.
