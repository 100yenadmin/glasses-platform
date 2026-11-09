# Device survey (November 2026)

This is the research behind milestones M4–M11 (one per device family) in [ROADMAP.md](../ROADMAP.md). It records which devices
could connect to the plugin, how, and what is blocking them. Every row comes from the linked primary source as of
2026-11-09. "Unverified" means we could not confirm it.

## Where the plugin fits

- Hermes core already has an OpenAI-compatible API server, so any device that only needs plain question-and-answer
  chat can use that today (for example
  [hermes-voice](https://github.com/majorminorlabs/hermes-voice) for Home Assistant speakers).
- This plugin is for what plain chat cannot do: pushes to the device (cron jobs, reminders, replies that finish
  later), photos on request, approvals answered on the device, token pairing, one session shared by all of a
  person's devices, and the offline queue.
- Devices differ in two ways: what they can do (camera, display, speaker, mic, buttons, haptics) and how they
  connect. The plugin supports three ways of connecting: the WebSocket protocol (today), an HTTP route for devices
  that cannot hold a WebSocket (today, 0.4.0), and compatible endpoints for existing device firmware (0.5.0: xiaozhi, Omi, Bobby).

## Glasses

| Device | How a client connects | What third-party code gets | Blocker | Plan |
|---|---|---|---|---|
| Ray-Ban Meta, Ray-Ban Display, Oakley ([toolkit](https://wearables.developer.meta.com/docs/develop/)) | A phone app using Meta's device toolkit | Camera, display (Display model); audio streaming is experimental | Developer preview; public publishing limited to partners | M2 via [hermes-glasses](https://github.com/prasanthsasikumar/hermes-glasses) |
| Even Realities G2 ([Even Hub](https://github.com/even-realities/everything-evenhub)) | A web app inside the Even phone app; network via a URL allowlist | Glasses mic (PCM 16 kHz), display, touchpad, ring, IMU; no camera | Public listing needs Even review; beta testers do not | M7 |
| Even Realities G1 ([demo app](https://github.com/even-realities/EvenDemoApp)) | A phone app speaking raw Bluetooth | Mic (LC3), display, touch bar | No app platform | Through MentraOS |
| Mentra Live, MentraOS ([MentraOS](https://github.com/Mentra-Community/MentraOS)) | Mentra phone app (miniapps) or the Mentra Live Bluetooth SDK | Transcripts, buttons, display; Mentra Live also camera, mic, speaker | Miniapps cannot be distributed yet | M11 |
| Brilliant Labs Halo ([SDK](https://github.com/brilliantlabsAR/brilliant_sdk)) | Lua on the glasses, Bluetooth to a phone or PC host | Display, audio, IMU, camera | Shipping status unverified | Watch list |
| Android XR glasses ([Jetpack Projected](https://developer.android.com/develop/xr/jetpack-xr-sdk/access-hardware-projected-context)) | A normal phone app with a projected context | Mic, camera, display | Alpha; consumer devices unverified | Watch list |
| Snap Specs ([WebSocket API](https://developers.snap.com/spectacles/about-spectacles-features/apis/web-socket)) | A Lens on the glasses opens `wss://` directly | Mic, camera, display (publishing rules unverified) | Consumer availability unverified | Watch list |

## Watches

| Device | How a client connects | Blocker | Plan |
|---|---|---|---|
| Apple Watch ([TN3135](https://developer.apple.com/documentation/technotes/tn3135-low-level-networking-on-watchos)) | HTTP(S) through `URLSession`; WebSockets are allowed only during audio streaming or calls. Pushes through Apple's notification service | No streamed replies; Shortcuts on the watch need a real-device test | M4 HTTP route (0.4.0), then M5 Shortcut and app |
| Wear OS ([network access](https://developer.android.com/training/wearables/data/network-access)) | Normal Android networking; WebSocket works | Battery: connect while in use | M9 Android client |
| Pebble ([PebbleOS](https://github.com/coredevices/PebbleOS), [Bobby](https://github.com/pebble-dev/bobby-assistant)) | Phone-side JavaScript with WebSocket support | Needs the phone app running | M10: point Bobby at our protocol |
| Garmin ([Connect IQ](https://developer.garmin.com/connect-iq/api-docs/Toybox/Communications.html)) | HTTP requests only, small responses | Microphone access is new (SDK 9.3) | Watch list |

## Speakers, voice boxes and pendants

| Device | How a client connects | Plan |
|---|---|---|
| ESP32 voice boxes running [xiaozhi](https://github.com/78/xiaozhi-esp32) (138+ boards) | WebSocket with a bearer token, a JSON hello, Opus audio frames, MCP messages ([protocol](https://github.com/78/xiaozhi-esp32/blob/main/docs/websocket.md)) | M6: a compatible endpoint |
| Home Assistant Voice PE, ESPHome satellites, hacked Echos, Nest board swaps | Home Assistant talks to the device; an agent plugs in as a conversation agent | Covered by hermes-voice / the Hermes API server |
| [Omi](https://docs.omi.me/doc/developer/apps/Integrations) pendant | Webhooks from Omi's backend: live transcript or raw audio | M8: webhook relay |

## Not planned

The Wyoming satellite project is archived (replaced by an ESPHome-based assistant). WebRTC realtime stacks for
ESP32 need a media server. Humane Ai Pin community firmware is discontinued. Brilliant Frame's Noa app has not
changed since May 2025. No open path was found for Plaud or Flipper.

## Other Hermes plugins in this space

- [hermes-mobile](https://github.com/geniok1980/hermes-mobile-adapter): a WebSocket gateway for phone apps, with
  approval cards and SDKs. Closest to this plugin; interoperating is worth exploring.
- [Hermes3DS](https://github.com/rndaom/Hermes3DS): a Nintendo 3DS gateway with encrypted pairing.
- [airpods-approvals](https://github.com/NousResearch/hermes-agent/blob/main/plugin-catalog/airpods-approvals.yaml):
  answer approvals with a nod or shake.
