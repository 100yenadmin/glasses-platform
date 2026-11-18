# Vision

## Goal

Your own Hermes agent on every device that is not a desktop: glasses first, then watches, voice boxes, pendants and
cars. Each person has one conversation shared by all of their devices. The agent sees through the device's camera,
answers by voice or on a lens, asks for approvals on the device, and sends reminders and other pushes to it later.
Devices differ only in what they can do and how they connect; the plugin stays one Hermes channel for all of them.

## Users

- **Hermes users with a device:** Even Realities G2 glasses, Ray-Ban Meta glasses through the community
  hermes-glasses app, ESP32 voice boxes on the xiaozhi firmware, the Omi pendant, Pebble watches through Bobby, and
  an Apple Watch or anything else that can run a Siri Shortcut or an HTTP request.
- **People building a device client:** the [protocol](docs/PROTOCOL.md), the [conformance kit](conformance/README.md)
  and the JavaScript and Python clients let a third-party app show that it works with Hermes.

What has shipped and what is next: [ROADMAP.md](ROADMAP.md). What has been checked: the README's device table (no real
device has been tested yet).

## Coverage on each Hermes surface

- **Messaging gateway (`hermes gateway`):** the home of the plugin. It registers the `glasses` platform, and the
  gateway gives it sessions, memory, tools, cron, heartbeat and approvals, as it does for Telegram or Slack.
- **Telegram and other messaging platforms:** separate channels with their own sessions. Glasses do not join a
  Telegram conversation. Cron jobs and notifications reach glasses when they are delivered to `glasses:<user>`.
- **CLI:** `hermes glasses pair | devices | revoke | try`. `try` talks to your agent from the terminal as a pair of
  glasses with a camera. The interactive chat (CLI or TUI) is not a glasses channel.
- **Desktop and `hermes serve`:** not checked. The plugin runs only inside a messaging gateway that loads plugins
  in-process (the default; `plugins.isolation: host` cannot load platform plugins).
- **API server:** not used. A device that can only call an OpenAI-compatible API (Even G2's built-in agent slot,
  Home Assistant voice) should use Hermes's own API server instead.

## Non-goals

- Device firmware, and phone apps for devices that already have a community app (hermes-glasses for Ray-Ban Meta).
- A hosted relay service: devices connect to your own gateway (LAN, Tailscale, or wss behind your own proxy).
- Home Assistant speakers and satellites, which hermes-voice or Hermes's API server cover.
- Direct Wyoming satellites, WebRTC realtime stacks, Echo- or Nest-specific firmware, Humane Ai Pin, Brilliant
  Frame/Noa, Plaud and Flipper (ROADMAP.md, "Not planned").
