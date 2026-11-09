# Hermes Glasses

![Hermes Glasses: your own Hermes agent, on your glasses](docs/media/banner.png)

Plugin id and repository: `glasses-platform`.

**Talk to your own Hermes agent from smart glasses.** Ask by voice, show it what you see, and hear or read the
answer. Reminders and long tasks can reach your glasses later.

> **Status: v0.4.0 preview.** The Hermes side is proven against the bundled simulator and a real Hermes
> gateway with real models ([evidence](docs/evidence/M0-live-proof.md)). CI tests it on Hermes v0.22.2, v0.23.0 and
> `main`. No real glasses or phone app has been tested yet. Read the [design plan](docs/PLAN.md), the
> [device protocol](docs/PROTOCOL.md) and the [roadmap](ROADMAP.md).

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

## Planned support

| Glasses | Path |
|---|---|
| Meta Ray-Ban (Gen 1/2), Ray-Ban Display, Oakley Meta | A phone app on Meta's Wearables Device Access Toolkit. The community [hermes-glasses](https://github.com/prasanthsasikumar/hermes-glasses) iOS app speaks protocol v0 in its bridge mode; not yet tested against this plugin on a device. |
| Even Realities G2 | Even Hub app (M4) |
| Mentra Live | Mentra cloud app (M4) |
| Meta AI on glasses | Meta AI Connectors endpoint (M4, gated on Meta early access) |

## Install

Requirements:
- Hermes Agent 0.22.2 or later. The plugin needs `aiohttp`, which is declared in `pyproject.toml`. If your Hermes
  was installed without the `messaging` extra, `hermes plugins install` asks to install it. Without a terminal,
  pass `--yes-deps`.
- In-process plugin loading, which is the default. The opt-in `plugins.isolation: host` mode cannot load platform plugins.

```bash
hermes plugins install https://github.com/100yenadmin/glasses-platform --enable
```

## Configure

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
`GLASSES_HOST`, `GLASSES_PORT` and the other settings are listed in [docs/PLAN.md](docs/PLAN.md#6-configuration).

To use it away from home Wi-Fi, put it behind Tailscale or a TLS reverse proxy. The plugin does not terminate TLS.

## Try it

**A demo gateway:** `demo/run_demo_gateway.sh <empty-dir>` starts an isolated Hermes gateway with this plugin, a
generated device token and only the vision, glasses, cronjob and memory toolsets (it uses the z.ai GLM coding plan;
see the script's header).

**From your terminal, in two minutes** (no glasses needed). `hermes glasses try` connects to your running gateway
as a pair of glasses with a camera. Type what you would say; reminders and other pushes print when they arrive:

```bash
hermes glasses pair            # device "glasses" for user "owner"
hermes gateway restart
hermes glasses try             # then: "What am I looking at?", "Remind me in one minute to stretch."
```

The camera "sees" a sample airport photo; pass `--photo my.jpg` for your own. The plugin asks the glasses for a photo
when you say something like "what am I looking at?", and the agent can take one itself with its
`glasses_capture_photo` tool. For scripted runs, `sim/glasses_sim.py --url "<connect URL>" --say "..." --pretty`
does the same without a prompt.

**With the hermes-glasses iOS app** (untested so far): in the app, set Settings → Assistant → Backend to Bridge,
and point it at `ws://<your-mac>.local:8765/voice?token=<token>`.

**Scheduled messages:** send them to your glasses with `--deliver glasses:<user>`, for example
`hermes cron create "every day at 9am" "Brief me on today" --deliver glasses:me`. They arrive as pushes, or wait in a
short offline queue until a device reconnects.

## What it does on your machine

- **Opens a network listener.** A WebSocket server on all interfaces, port 8765, path `/voice`, plus the HTTP
  routes `/turn`, `/pending` and `/approval` on the same port and with the same tokens (set `GLASSES_HOST`
  to `127.0.0.1` to keep it local). It refuses to start until at least one device token is configured.
- **A device token is full access to your agent.** Anyone holding one can chat with your agent as you, and can answer
  the approval prompts and clarify questions the agent sends to your devices. Keep tokens long, random and private.
- **Photos.** Photos from the glasses are saved in Hermes's own image cache, like photos sent from any other channel.
- **New session.** The device's new-session button answers Hermes's `/new` confirmation for that user's own session.
- **Approvals by voice or button.** When the agent wants to run a dangerous command, the glasses ask a short question
  ("Hermes wants to run: … Say yes to allow it once, or no to block it."). Only a whole "yes" (allow once, never for
  the session or always) or "no" from one of that user's own devices answers it; anything else goes to the agent as
  usual. Devices with buttons get the same approval as a card and can pick any choice Hermes offers for that request.
  Typed `/approve` and `/deny` still work. The plugin never answers an approval by itself.
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

## License

MIT. See [LICENSE](LICENSE).

## Build a companion client

The dependency-free [JavaScript client](clients/js/README.md) speaks protocol v1 over
WebSocket or HTTP, with injectable transports for phone runtimes. The
[conformance kit](conformance/README.md) checks a server or runs a scripted local
server for client authors. Planned server features still depend on the installed
server's advertised capabilities.
