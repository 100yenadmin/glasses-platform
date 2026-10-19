# glasses-platform — design plan

Status: **M0 planning** (2026-10-19). No code has landed yet. See [ROADMAP.md](../ROADMAP.md) for milestones and
[PROTOCOL.md](PROTOCOL.md) for the device wire protocol.

## 1. Problem

People want to talk to *their own* Hermes agent from smart glasses: ask by voice, show it what they are looking
at, and hear or read the answer. Results that arrive later should reach the glasses too: reminders, cron jobs,
long tasks.

Today there is no Hermes plugin for this. None of the ~700 entries in the upstream plugin catalog covers glasses,
and no upstream issue or PR asks for one (checked 2026-10-19). Community bridges exist outside the catalog, and
each re-solves the same problems on its own:

- [prasanthsasikumar/hermes-glasses](https://github.com/prasanthsasikumar/hermes-glasses) (MIT) is an iOS app on
  Meta's Wearables Device Access Toolkit (DAT). It targets Ray-Ban Meta, Ray-Ban Display, and sub-$100
  Realtek-based glasses. A Mac WebSocket bridge starts a new `hermes chat -q` process for every turn.
- [hightouch666/hermes-glasses-bridge](https://github.com/hightouch666/hermes-glasses-bridge) is an async task
  queue plus an MCP server for a Chinese AI-glasses platform. It shells out to `hermes -z`.
- [Intent-Lab/VisionClaw](https://github.com/Intent-Lab/VisionClaw) connects Ray-Ban Meta to Gemini Live and
  OpenClaw. It has no Hermes support.

Running the CLI once per turn cannot reach the glasses after the turn ends. It also loses streaming and
approvals, and reloads the agent on every utterance.

## 2. Goal and non-goals

**Goal:** one hardware-agnostic Hermes plugin that adds **smart glasses as a first-class gateway channel**.
Thin companion apps (one per glasses family) connect to it. Hermes keeps the session, memory, tools, cron,
heartbeat and approvals.

**Non-goals for M0:**
- Writing or shipping the device apps.
- Server-side TTS.
- Native approval frames.
- The Meta AI Connectors endpoint.
- A catalog entry.

The roadmap places each of these in a later milestone.

## 3. Hardware landscape (2026-10-19)

| Family | Developer path | Fit |
|---|---|---|
| **Meta Ray-Ban Gen 1/2, Oakley Meta HSTN/Vanguard** | [DAT 1.0](https://developers.meta.com/wearables/faq/) for iOS and Android: camera, microphone, audio, IMU. Builds can only be shared with testers; public publishing is not open yet. | First target, through a phone app |
| **Meta Ray-Ban Display** | DAT plus Display; [web apps](https://developers.meta.com/blog/meta-connect-recap-ai-glasses/) by URL (developer mode) | First target; adds lens text |
| **Ray-Ban Meta Gen 3 / Audio** (Connect 2026) | Not yet on DAT's supported list | Wait for Meta |
| **Meta AI Connectors** | Early access: your API or MCP server becomes tools that Meta AI calls | Later milestone (§9) |
| **Even Realities G2 (+ R1 ring)** | [Even Hub](https://hub.evenrealities.com/docs/getting-started/installation) TypeScript SDK; apps run in the phone app's WebView | Second client (HUD, no camera) |
| **Mentra Live** | Open-source MentraOS; [cloud SDK](https://docs.mentraglass.com/app-devs/getting-started) (`@mentra/sdk`) runs apps server-side | Second client (camera, no display) |
| **Android XR glasses, Brilliant Halo, Snap Specs** | Preview SDKs or not shipping yet | Watch |

The phone app is the device's bridge in every case. The plugin therefore speaks one protocol to "a companion
app" and never to the glasses directly.

## 4. Architecture

```
 glasses ──BLE/SDK──▶ companion app (phone / Even Hub / Mentra cloud)
                          │  STT on device, camera, display, speaker
                          │  WebSocket  wss://host:8765/voice   (PROTOCOL.md)
                          ▼
 ┌──────────────────── Hermes gateway process ───────────────────────┐
 │  glasses-platform plugin                                          │
 │   GlassesAdapter(BasePlatformAdapter)                             │
 │     aiohttp listener ─ device registry ─ outbox ─ photo broker    │
 │     handle_message(MessageEvent) ─────▶ GatewayRunner ─▶ agent    │
 │     send()/edit_message() ◀──────────── replies, cron, heartbeat  │
 │   tool: glasses_capture_photo (toolset "glasses")                 │
 └───────────────────────────────────────────────────────────────────┘
```

## 5. Decisions

**D1. A gateway platform plugin, not the API server.**
- The plugin is `kind: platform` and registers through `ctx.register_platform(...)`, so it needs no core edits.
- A platform adapter inherits sessions, memory, cron and heartbeat delivery, approvals, streaming and authz.
- The OpenAI-compatible API server is request/response and opts out of async delivery, so it cannot push a
  result to the glasses later.

**D2. Plugin id `glasses-platform`, platform name `glasses`.**
- When a load fails, Hermes's plugin loader recovers the platform name by stripping `-platform` from the manifest name.
- The repo, plugin id, folder and manifest name are the same string.
- Cron and `send` targets are `glasses` (the home user) and `glasses:<user>`.

**D3. Two protocol versions, both on one endpoint.**
- **v0** is wire-compatible with the hermes-glasses iOS bridge, so that app works unchanged.
- **v1** is negotiated with a `hello` frame. It adds streaming deltas, pushes, status, request ids and inline images.

Details are in [PROTOCOL.md](PROTOCOL.md).

**D4. One session per person, shared across that person's devices.**
- Each token maps to `user:device`.
- `chat_id = user_id = <user>`, so Ray-Ban, G2 and phone share one conversation and memory.
- A turn's reply goes to the device that asked.
- Pushes fan out to all of the user's live devices.

**D5. Token auth, fail closed.**
- Tokens are configured per device.
- They are compared with `hmac.compare_digest`, taken from `?token=` (v0) or `Authorization: Bearer` (preferred).
- If no tokens are configured, the listener refuses to start.
- A failed check closes the WebSocket with code `4401`.
- Because every connection is token-authenticated, the adapter sets `authorization_is_upstream` (as the A2A
  platform does). The token list is the allowlist.

**D6. Photos have two paths.**
- (a) **Keyword pre-capture**, ported from the hermes-glasses bridge heuristic ("what am I looking at", "read
  this", ...). It is on by default and can be turned off. The adapter sends `capture_photo` to the asking device
  and waits up to 25 s. It caches the JPEG with Hermes's image cache and dispatches a `PHOTO` message. The model
  sees the image natively, or Hermes pre-analyses it when the model has no vision.
- (b) The **`glasses_capture_photo` tool** lets the agent ask for a photo itself. It finds the device from the
  session context and returns a multimodal tool result, with a text fallback.

**D7. Push and offline delivery.**
- If a turn is active for the user, `send()` emits `response`.
- Otherwise it emits `push` (v1) or `response` (v0).
- If no device is connected, the message goes to a bounded in-memory outbox: 50 items, 24 h, flushed on reconnect.
- The outbox is lost on gateway restart in M0. M1 makes it durable, with acks.

**D8. Short spoken answers.** The platform hint tells the model that replies are spoken or shown on a tiny lens:
1–3 short sentences, no markdown, no tables, no raw URLs unless asked.

**D9. The Meta AI Connectors endpoint lives on the same listener.** `POST /mcp` would be served by the adapter's
own HTTP listener, inside the gateway process. It dispatches in-process, with no API-server hop and no dashboard
auth seam (see §9).

## 6. Configuration

Settings come from `platforms.glasses.extra` in `config.yaml` or from env vars. Env vars are read through Hermes's
scoped-secret helpers, never `os.getenv`.

| Setting | Env | Default | Meaning |
|---|---|---|---|
| devices | `GLASSES_DEVICES` | — | `user:device=token,…` |
| token | `GLASSES_TOKEN` | — | Shorthand for `owner:default=<token>` |
| host / port / path | `GLASSES_HOST` / `GLASSES_PORT` / `GLASSES_PATH` | `0.0.0.0` / `8765` / `/voice` | Listener |
| home user | `GLASSES_HOME_CHANNEL` | first user | Target for `deliver=glasses` |
| auto capture | `GLASSES_AUTO_CAPTURE` | `keywords` | `keywords` or `off` |
| photo timeout | `GLASSES_PHOTO_TIMEOUT` | `25` | Seconds |
| max frame | `GLASSES_MAX_FRAME_MB` | `8` | Largest WebSocket frame |
| outbox | `GLASSES_OUTBOX_MAX` / `GLASSES_OUTBOX_TTL_HOURS` | `50` / `24` | Offline queue |

## 7. Streaming, TTS and approvals

- **Streaming (M0).** With Hermes `streaming.enabled: true`, the gateway calls `send()` and then `edit_message()`
  with cumulative text. The adapter maps these to v1 `response_delta` frames and a final `response`. v0 clients
  receive only the final `response`.
- **TTS (M1).** In M0 the device speaks the reply text itself (`response.tts = false`; the hermes-glasses app
  already does this). M1 adds server-side audio: PCM16 mono 24 kHz in `audio_start` / binary / `audio_end`
  frames, using Hermes's `send_voice` and streaming-TTS hooks. Turns where the user spoke arrive as `VOICE`
  messages carrying the transcript, so Hermes auto-TTS applies.
- **Approvals.** In M0 the text fallback works with no code: the user says or taps "/approve". M1 adds native
  `approval_request` / `approval_response` frames, resolved through Hermes's gateway approval API.

## 8. Networking and exposure

- **Same Wi-Fi:** `ws://<host>.local:8765/voice`.
- **Away from home:** Tailscale, or a TLS reverse proxy that terminates `wss://`. The plugin does not terminate TLS.
- Prefer the bearer header over `?token=`, because proxies log query strings.

## 9. Meta AI Connectors (M4, gated on Meta early access)

Meta AI on the glasses would call our MCP tools:
- `ask_hermes(task, image_url?)`
- `hermes_task_status(task_id)`

Calls from the glasses time out, so the flow is **accept fast, finish slow**. The endpoint dispatches a
`MessageEvent` to the connector user's session and waits up to N seconds. If the reply is done it returns it;
otherwise it returns a task id. The final reply is stored for `hermes_task_status` and also pushed to any live
device of that user.

Why on the adapter's listener: plugin dashboard routes run in a different process than the gateway, and its
machine-token seam would also authorise unrelated routes. The auth model (OAuth account linking or a service key)
waits on Meta's connector documentation.

## 10. Client compatibility

| Client | M0 status | Change needed |
|---|---|---|
| hermes-glasses iOS (Ray-Ban Meta / Display / Realtek) | v0 works by pointing the app at our URL and token | v1 (deltas, push, inline photo) is a small client PR or fork (M2) |
| Even Realities G2 (Even Hub app) | — | New thin TypeScript client (M3) |
| Mentra Live (cloud app) | — | New thin `@mentra/sdk` app (M3) |
| Python simulator (`sim/glasses_sim.py`) | Ships in M0 | Reference client and test driver |

## 11. Constraints

- **In-process plugin loading only.** Hermes's optional `plugins.isolation: host` refuses `register_platform`.
- **Default profile only** for now. A plugin platform is not in Hermes's shared multiplex port binding, so a
  second profile would need its own port.
- **Needs `aiohttp`** (Hermes `messaging` extra). The platform's `check_fn` reports when it is missing.

## 12. M0 deliverables

```
plugin.yaml        kind: platform, label, requires_env / optional_env
__init__.py        register(ctx)
adapter.py         GlassesAdapter: listener, auth, dispatch, send/edit/typing, push routing
protocol.py        v0/v1 frame parsing and building
devices.py         token → user:device, connection registry, outbox
capture.py         keyword heuristic, photo request/await broker
tools.py           glasses_capture_photo
sim/glasses_sim.py CLI simulator (legacy or v1; answers capture_photo from a JPEG; waits for pushes)
tests/             unit + gated integration (real gateway, fake LLM)
docs/              PLAN.md, PROTOCOL.md
```

## 13. Verification plan (M0 exit)

1. **Unit tests**, with the plugin loaded the way Hermes loads it and a fake runner:
   - registration kwargs
   - auth: none, wrong, query, bearer, and fail-closed start
   - v0 query → `MessageEvent` shape; response routing; push vs response
   - two-user isolation; outbox flush
   - capture round trip and timeout
   - `new_session` → `/new`
   - v1 hello and deltas
   - the tool with and without a device
2. **Integration test** (opt-in): a real `python -m gateway.run` in a throwaway `HERMES_HOME` with a scripted
   fake OpenAI-compatible model.
   - (a) A text turn gets a reply frame.
   - (b) A keyword photo turn sends `capture_photo`; the simulator's JPEG reaches the model request; a reply comes back.
   - (c) A one-shot cron job delivers a `push` frame.
   - (d) SIGTERM drains cleanly.
3. `hermes plugins validate` passes.
4. **Live proof:** a real gateway with a real model. The simulator runs a text turn, a photo turn and a cron
   push, in both v1 and legacy v0 mode.

**What M0 does NOT prove:** real glasses, any phone app run, any Meta account or DAT build, or production
networking.

## 14. Open questions

- What auth do Meta AI Connectors expect (OAuth account linking or service key), and what is their tool-call timeout?
- Should the device photo be retained only in Hermes's image cache (current plan), or also attached to the session transcript?
- Should the hermes-glasses author be invited to adopt v1 upstream, rather than us forking? Asking is preferred.
