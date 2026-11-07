# glasses-platform

**Talk to your own Hermes agent from smart glasses.** Ask by voice, show it what you see, and hear or read the
answer. Reminders and long tasks can reach your glasses later.

> **Status: v0.1.0 preview (milestone M0).** The Hermes side is proven against the bundled simulator and a real Hermes
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

## Planned support

| Glasses | Path |
|---|---|
| Meta Ray-Ban (Gen 1/2), Ray-Ban Display, Oakley Meta | A phone app on Meta's Wearables Device Access Toolkit. The community [hermes-glasses](https://github.com/prasanthsasikumar/hermes-glasses) iOS app speaks protocol v0 in its bridge mode; not yet tested against this plugin on a device. |
| Even Realities G2 | Even Hub app (M4) |
| Mentra Live | Mentra cloud app (M4) |
| Meta AI on glasses | Meta AI Connectors endpoint (M4, gated on Meta early access) |

## Install

Requirements:
- Hermes Agent 0.22.2 or later, with the `messaging` extra, which provides `aiohttp`.
- In-process plugin loading, which is the default. The opt-in `plugins.isolation: host` mode cannot load platform plugins.

```bash
hermes plugins install https://github.com/100yenadmin/glasses-platform --enable
```

## Configure

Give each device its own token. Tokens map to `user:device`, and all devices of one user use the same conversation.
Put this in `~/.hermes/.env`:

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

Photo turns need a model that can see: either a main model that accepts images, or an `auxiliary.vision` model in
`config.yaml`.

The plugin refuses to start without at least one token. It listens on `0.0.0.0:8765` at `/voice` by default.
`GLASSES_HOST`, `GLASSES_PORT` and the other settings are listed in [docs/PLAN.md](docs/PLAN.md#6-configuration).

To use it away from home Wi-Fi, put it behind Tailscale or a TLS reverse proxy. The plugin does not terminate TLS.

## Try it

**With the simulator:**

```bash
GLASSES_SIM_TOKEN=<token> python sim/glasses_sim.py --url ws://127.0.0.1:8765/voice --say "What can you do?"
```

To test a photo turn, add `--photo photo.jpg`. The plugin asks the "glasses" for a photo when you say something like
"what am I looking at?". The agent can also take one itself with the `glasses_capture_photo` tool.

**With the hermes-glasses iOS app** (untested so far): in the app, set Settings → Assistant → Backend to Bridge,
and point it at `ws://<your-mac>.local:8765/voice?token=<token>`.

**Scheduled messages:** send them to your glasses with `--deliver glasses:<user>`, for example
`hermes cron create "every day at 9am" "Brief me on today" --deliver glasses:me`. They arrive as pushes, or wait in a
short offline queue until a device reconnects.

## What it does on your machine

- **Opens a network listener.** A WebSocket server on all interfaces, port 8765, path `/voice` (set `GLASSES_HOST`
  to `127.0.0.1` to keep it local). It refuses to start until at least one device token is configured.
- **A device token is full access to your agent.** Anyone holding one can chat with your agent as you, and can answer
  the approval prompts and clarify questions the agent sends to your devices. Keep tokens long, random and private.
- **Photos.** Photos from the glasses are saved in Hermes's own image cache, like photos sent from any other channel.
- **New session.** The device's new-session button answers Hermes's `/new` confirmation for that user's own session.
  The plugin never answers command approvals; the wearer does.
- **No third-party calls.** The plugin makes no outbound network requests, runs no shell commands, sends no telemetry
  and starts no background processes beyond its listener.
- **Tokens** are read through Hermes's per-profile settings reader, from `~/.hermes/.env` or `config.yaml`.
- **Offline queue.** Pushes for a disconnected device wait in memory (50 items, 24 hours) and are lost on a gateway
  restart.

## Why a plugin rather than a bridge script

- **Existing bridges** start a new Hermes CLI process for every utterance. They cannot reach the glasses after the
  turn ends, and they lose streaming and approvals.
- **A gateway platform** keeps one live session per person across all their devices. Cron and heartbeat results
  arrive as pushes, and replies stream to a heads-up display.

## License

MIT. See [LICENSE](LICENSE).
