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

- [ ] Server-side TTS (PCM16 24 kHz) through Hermes TTS providers; auto-TTS for voice turns
- [ ] Native approval frames (`approval_request` / `approval_response`)
- [ ] `interrupt` → `/stop`
- [ ] Durable outbox with `ack`
- [ ] `hermes glasses pair <user> <device>`: mint a device token and print a QR / connect URL
- [ ] CI matrix (pinned Hermes release + main), community kit, product-page README

**Exit criteria:** every M1 frame is covered by tests, and the live proof covers TTS and approvals.

## M2 — First real glasses: Meta Ray-Ban via hermes-glasses

- [ ] Run the hermes-glasses iOS app unchanged against the plugin (v0) on real Ray-Ban Meta glasses
- [ ] Propose v1 support to hermes-glasses upstream (deltas, push, inline photo); fork only if declined
- [ ] Real-device proof: voice turn, photo turn, a push arriving while idle, a Ray-Ban Display lens render

**Exit criteria:** recorded real-device sessions with pass rates. Simulator runs do not count.

## M3 — Catalog 1.0

- [ ] Hermes plugin catalog entry: pinned sha, house-style banner, real screenshots, a disclosure sentence
- [ ] Release notes, security model and compatibility table

**Exit criteria:** the catalog PR is merged and `hermes plugins install glasses-platform` works from the catalog.

## M4 — More glasses and Meta AI

- [ ] Even Realities G2 client (Even Hub app)
- [ ] Mentra Live client (`@mentra/sdk` cloud app)
- [ ] Meta AI Connectors MCP endpoint (`ask_hermes`, `hermes_task_status`), gated on Meta early access
- [ ] Ray-Ban Display web app (by URL)
- [ ] Watch list: Android XR glasses (Jetpack Projected), Brilliant Halo, Snap Specs, Ray-Ban Meta Gen 3 once DAT supports it
