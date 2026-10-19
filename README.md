# glasses-platform

**Talk to your own Hermes agent from smart glasses.** Ask by voice, show it what you see, and hear or read the
answer. Reminders and long tasks can reach your glasses later.

> **Status: M0 preview.** The Hermes side works against the bundled simulator. No real glasses or phone app has been
> tested yet. Read the [design plan](docs/PLAN.md), the [device protocol](docs/PROTOCOL.md) and the
> [roadmap](ROADMAP.md).

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
| Meta Ray-Ban (Gen 1/2), Ray-Ban Display, Oakley Meta | A phone app on Meta's Wearables Device Access Toolkit. The community [hermes-glasses](https://github.com/prasanthsasikumar/hermes-glasses) iOS app is protocol-compatible (v0). |
| Even Realities G2 | Even Hub app (M4) |
| Mentra Live | Mentra cloud app (M4) |
| Meta AI on glasses | Meta AI Connectors endpoint (M4, gated on Meta early access) |

## Install (M0 preview)

Requirements:
- Hermes Agent with the `messaging` extra, which provides `aiohttp`.
- In-process plugin loading, which is the default. The opt-in `plugins.isolation: host` mode cannot load platform plugins.

```bash
hermes plugins install https://github.com/100yenadmin/glasses-platform --enable
```

## Configure

Give each device its own token. Tokens map to `user:device`, and all devices of one user share one conversation.
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

**With the hermes-glasses iOS app:** in the app, set Settings → Assistant → Backend to Bridge, and point it at
`ws://<your-mac>.local:8765/voice?token=<token>`.

**Scheduled messages:** send them to your glasses with `--deliver glasses:<user>`, for example
`hermes cron create "every day at 9am" "Brief me on today" --deliver glasses:me`. They arrive as pushes, or wait in a
short offline queue until a device reconnects.

## Why a plugin rather than a bridge script

- **Existing bridges** start a new Hermes CLI process for every utterance. They cannot reach the glasses after the
  turn ends, and they lose streaming and approvals.
- **A gateway platform** keeps one live session per person across all their devices. Cron and heartbeat results
  arrive as pushes, and replies stream to a heads-up display.

## License

MIT. See [LICENSE](LICENSE).
